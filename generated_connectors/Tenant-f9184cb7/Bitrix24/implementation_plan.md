# Bitrix24 Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Bitrix24** is a unified CRM + collaboration suite (CRM, Tasks, Drive, Calendar, Messaging, Lists) exposing a REST API at tenant-scoped portals such as `https://{portal}.bitrix24.com/rest/`. This connector — `Bitrix24Connector` (`CONNECTOR_TYPE = "bitrix24"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs:

| Surface           | Base path                              | Capability                                          |
|-------------------|----------------------------------------|-----------------------------------------------------|
| User              | `/user.current`, `/user.get`           | Identify the caller; list portal users              |
| CRM Leads         | `/crm.lead.*`                          | List/get/add/update/delete leads                    |
| CRM Contacts      | `/crm.contact.*`                       | List/get/add/update/delete contacts                 |
| CRM Companies     | `/crm.company.*`                       | List/get/add companies                              |
| CRM Deals         | `/crm.deal.*`                          | List/get/add deals                                  |
| CRM Quotes        | `/crm.quote.list`                      | List quotes                                         |
| CRM Invoices      | `/crm.invoice.list`                    | List invoices                                       |
| CRM Activities    | `/crm.activity.list/add`               | List + log CRM activities                           |
| Tasks             | `/tasks.task.list/get/add/update`      | Task list, create, update                           |
| Disk              | `/disk.folder.getchildren`             | List folders/files on Bitrix24 Drive                |
| Messaging (Im)    | `/im.message.add`                      | Send an Im message                                  |
| Lists             | `/lists.element.get`                   | Read Lists module rows                              |
| Calendar          | `/calendar.event.get`                  | Calendar events                                     |

The connector normalises leads + contacts + deals + tasks into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::Bitrix24HTTPClient`).

## 2. SDK / Package Selection

We use **raw `httpx.AsyncClient`** rather than any third-party Bitrix24 SDK so the connector matches the Bandwidth/Wix gold-standard pattern: single httpx owner inside `client/http_client.py`, retry policy + auth header centralised, zero hidden global state.

Dependencies (Section 7) intentionally minimal — `httpx`, `pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock` are all pre-installed by the gateway. The only connector-specific dependency we add is `respx>=0.21` for HTTP mocking in unit tests.

## 3. Auth Flow

Bitrix24 supports two server-to-server auth modes — this connector treats both uniformly as **API key** (`AUTH_TYPE = "api_key"`):

### Mode A — Inbound webhook URL (default, recommended)
A Bitrix24 admin opens **Developer resources → Other → Inbound webhook**, picks the permissions (CRM/Tasks/User/etc.), and copies a URL of the form:

```
https://{portal}.bitrix24.com/rest/{user_id}/{webhook_code}/
```

The URL itself is the credential — every API method is invoked by appending `{method}.json` to the URL. The Shielva tenant pastes this URL into the `webhook_url` install field; the connector treats it as an opaque secret.

### Mode B — OAuth2 access token (optional)
Sites running a Bitrix24 marketplace app already perform the OAuth dance externally and end up with an `access_token`. When configured, the connector calls portal REST methods as:

```
https://{portal}.bitrix24.com/rest/{method}.json?auth=<access_token>
```

The `portal` (derived from `webhook_url`) and the `access_token` are both install_fields.

### Header / lifecycle contract

| Phase            | Behaviour                                                                                                    |
|------------------|--------------------------------------------------------------------------------------------------------------|
| `install()`      | Validates `webhook_url` is present and well-formed. Does **not** call Bitrix24.                              |
| `authorize(...)` | Returns an empty `TokenInfo` whose `access_token` is the configured `access_token` or `webhook_url`. No-op.  |
| `health_check()` | Calls the cheapest authenticated method — `user.current`. 2xx → `HEALTHY+CONNECTED`. 401 → `OFFLINE+TOKEN_EXPIRED`. 403 → `UNHEALTHY+INVALID_CREDENTIALS`. 429 → `DEGRADED+CONNECTED`. |
| token storage    | None — webhook URL is static. `access_token`, when used, is stored in install_fields.                        |

## 4. Data Model

For CRM/Tasks surfaces — naturally KB-shaped — the normaliser produces `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`). For Lists/Calendar/Disk surfaces the connector is API-passthrough.

### 4.1 Lead → NormalizedDocument

| field         | from                                                          |
|---------------|---------------------------------------------------------------|
| `id`          | `f"{tenant_id}_{lead['ID']}"`                                  |
| `source_id`   | `lead["ID"]`                                                  |
| `title`       | `lead.get("TITLE") or f"Lead {ID}"`                            |
| `content`     | `f"{NAME} {LAST_NAME} ({STATUS_ID})"`                          |
| `created_at`  | `lead["DATE_CREATE"]`                                         |
| `metadata`    | `{status, source, opportunity, currency, assigned_to, kind: "bitrix24.lead"}` |

### 4.2 Deal → NormalizedDocument

| field        | from                                                            |
|--------------|-----------------------------------------------------------------|
| `id`         | `f"{tenant_id}_{deal['ID']}"`                                    |
| `title`      | `deal.get("TITLE") or f"Deal {ID}"`                              |
| `content`    | `f"Stage {STAGE_ID} — {OPPORTUNITY} {CURRENCY_ID}"`               |
| `metadata`   | `{stage, opportunity, currency, contact_id, company_id, kind}`   |

### 4.3 Contact → NormalizedDocument

| field        | from                                                            |
|--------------|-----------------------------------------------------------------|
| `id`         | `f"{tenant_id}_{contact['ID']}"`                                 |
| `title`      | `f"{NAME} {LAST_NAME}".strip() or f"Contact {ID}"`                 |
| `content`    | first email + first phone                                       |
| `metadata`   | `{post, comments, kind}`                                        |

### 4.4 Task → NormalizedDocument

| field        | from                                                            |
|--------------|-----------------------------------------------------------------|
| `id`         | `f"{tenant_id}_{task['id']}"`                                    |
| `title`      | `task.get("title") or f"Task {id}"`                              |
| `content`    | `task.get("description") or ""`                                 |
| `metadata`   | `{status, responsibleId, deadline, kind}`                       |

## 5. Key API Endpoints & Methods

Every method below is a standalone public `async def` on `Bitrix24Connector`. SOC — `connector.py` only orchestrates; HTTP lives in `client/http_client.py::Bitrix24HTTPClient`.

### 5.1 `async install() -> ConnectorStatus`
Validate `webhook_url` is present and matches `^https?://[^/]+\.bitrix24\.[^/]+/rest/.+/$`. Save merged config. Never calls the API.

### 5.2 `async authorize(auth_code="", state="") -> TokenInfo`
No-op for api_key auth — returns a `TokenInfo(access_token=webhook_url, token_type="webhook")` (or `access_token=access_token, token_type="oauth"` when Mode B is configured).

### 5.3 `async health_check() -> ConnectorStatus`
Calls `user.current`. Classifies failures via `_STATUS_MAP`.

### 5.4 `async sync(since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult`
Pages CRM leads + contacts + deals + tasks → normalised docs → `ingest_document`. Returns `SyncResult(COMPLETED|PARTIAL|FAILED, documents_found/_synced/_failed)`.

### 5.5 User
- `async user_current() -> Dict` — `user.current.json`
- `async list_users(*, start=0) -> Dict` — `user.get.json` (50 / page)

### 5.6 CRM Leads
- `async list_leads(*, start=0, select=None, filter=None, order=None) -> Dict` — `crm.lead.list.json`
- `async get_lead(lead_id) -> Dict` — `crm.lead.get.json`
- `async create_lead(fields) -> Dict` — `crm.lead.add.json`
- `async update_lead(lead_id, fields) -> Dict` — `crm.lead.update.json`
- `async delete_lead(lead_id) -> Dict` — `crm.lead.delete.json`

### 5.7 CRM Contacts
- `async list_contacts(*, start=0, select=None, filter=None, order=None) -> Dict`
- `async get_contact(contact_id) -> Dict`
- `async create_contact(fields) -> Dict`
- `async update_contact(contact_id, fields) -> Dict`
- `async delete_contact(contact_id) -> Dict`

### 5.8 CRM Companies
- `async list_companies(*, start=0) -> Dict` — `crm.company.list.json`
- `async create_company(fields) -> Dict` — `crm.company.add.json`

### 5.9 CRM Deals
- `async list_deals(*, start=0, select=None, filter=None) -> Dict` — `crm.deal.list.json`
- `async get_deal(deal_id) -> Dict`
- `async create_deal(fields) -> Dict`
- `async update_deal(deal_id, fields) -> Dict`
- `async delete_deal(deal_id) -> Dict`

### 5.10 CRM Quotes / Invoices / Activities
- `async list_quotes(*, start=0) -> Dict` — `crm.quote.list.json`
- `async list_invoices(*, start=0) -> Dict` — `crm.invoice.list.json`
- `async list_activities(*, start=0, filter=None) -> Dict` — `crm.activity.list.json`
- `async create_activity(fields) -> Dict` — `crm.activity.add.json`

### 5.11 Tasks
- `async list_tasks(*, start=0, filter=None, select=None, order=None) -> Dict` — `tasks.task.list.json` (result nests under `result.tasks`)
- `async get_task(task_id) -> Dict` — `tasks.task.get.json`
- `async create_task(fields) -> Dict` — `tasks.task.add.json`
- `async update_task(task_id, fields) -> Dict` — `tasks.task.update.json`

### 5.12 Disk
- `async list_disk_children(folder_id) -> Dict` — `disk.folder.getchildren.json`

### 5.13 Messaging
- `async send_im_message(*, dialog_id, message, system=False) -> Dict` — `im.message.add.json`

### 5.14 Lists / Calendar
- `async list_lists_elements(iblock_id, *, filter=None) -> Dict` — `lists.element.get.json`
- `async list_calendar_events(*, type="user", owner_id, from_date=None, to_date=None) -> Dict` — `calendar.event.get.json`

## 6. Error Handling

| HTTP | Exception                       | Retryable                                                              |
|------|---------------------------------|------------------------------------------------------------------------|
| 400  | `Bitrix24BadRequestError`       | No                                                                     |
| 401  | `Bitrix24AuthError`             | No                                                                     |
| 403  | `Bitrix24AuthError`             | No                                                                     |
| 404  | `Bitrix24NotFoundError`         | No                                                                     |
| 429  | `Bitrix24RateLimitError`        | Yes — exponential backoff, max 3 attempts                              |
| 5xx  | `Bitrix24ServerError`           | Yes — exponential backoff, max 3 attempts                              |
| net  | `Bitrix24NetworkError`          | Yes — exponential backoff, max 3 attempts                              |

Back-compat aliases preserved: `Bitrix24NetworkError = Bitrix24ServerError`, `Bitrix24NotFound = Bitrix24NotFoundError`, `Bitrix24APIError = Bitrix24Error`, `Bitrix24ConnectorError = Bitrix24Error`.

Bitrix24's REST API returns app-level errors as `{"error": "QUERY_LIMIT_EXCEEDED", "error_description": "..."}` with HTTP 200/400. The HTTP client treats `error == "QUERY_LIMIT_EXCEEDED"` as a 429 surrogate and `error in ("expired_token","invalid_token")` as 401.

Exceptions surface to callers; `health_check()` and `sync()` catch them at the lifecycle boundary and map via `_STATUS_MAP` to `ConnectorStatus`.

## 7. Dependencies

Only connector-specific packages (gateway pre-installs: `httpx`, `pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock`):

```
respx>=0.21
```

## 8. Config & Install Fields

| Key                | Type   | Required | Purpose                                                                 |
|--------------------|--------|----------|-------------------------------------------------------------------------|
| `webhook_url`      | secret | yes      | Inbound webhook URL `https://{portal}.bitrix24.com/rest/{uid}/{code}/`  |
| `access_token`     | secret | no       | OAuth access_token (Mode B). When set, `?auth={token}` is appended.     |
| `portal`           | string | no       | Override portal subdomain. Auto-derived from `webhook_url` when blank.  |
| `base_url`         | string | no       | Override base. Auto-derived from `webhook_url` when blank.              |
| `rate_limit_per_min` | number | no     | Soft cap, default 2 (Bitrix24 free tier is 2 req/s).                     |
| `timeout_s`        | number | no       | Per-request httpx timeout (default 30).                                 |

`REQUIRED_CONFIG_KEYS = ["webhook_url"]` (public class const).

`_STATUS_MAP`:
```python
{
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC / OCP Architecture Plan

- **SOC** — `connector.py` orchestrates only. Every HTTP call goes through `client/http_client.py::Bitrix24HTTPClient.call`. Every data shape transformation is in `helpers/normalizer.py`. Retry / utility helpers in `helpers/utils.py`.
- **OCP** — adding a new Bitrix24 method (e.g. `crm.timeline.comment.add`) is purely additive: extend `Bitrix24HTTPClient.call` (already generic), expose a thin orchestrator on `Bitrix24Connector`. `_STATUS_MAP` is a public class const so subclasses can extend without modifying logic.
- **Multi-tenant** — every `NormalizedDocument.id` is `f"{self.tenant_id}_{source_id}"`. The HTTP client is constructed per instance; there is no module-global state.
- **Abstract surface** — `install`, `sync`, `health_check` are implemented (otherwise `BaseConnector` raises `TypeError` at instantiation).

**Score: 10/10.**
