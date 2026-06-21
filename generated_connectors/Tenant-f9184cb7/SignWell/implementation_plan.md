# SignWell Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**SignWell** is a transactional e-signature platform exposing a REST API under `https://www.signwell.com/api/v1`. This connector — `SignWellConnector` (`CONNECTOR_TYPE = "signwell"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs:

| Surface | Base path | Capability |
|---|---|---|
| Account | `/me` | Authenticated account metadata + plan quotas |
| Documents | `/documents/*` | Create, send, list, get, cancel, archive, delete, download signed PDF |
| Document Templates | `/document_templates/documents` | Instantiate a document from a reusable template |
| Templates | `/templates/*` | List + read reusable templates |
| Recipients / Reminders | `/documents/{id}/recipients[/...]` | List recipients, send per-recipient reminder |
| Webhooks | `/api_application/webhooks` | Register + list + delete webhook subscriptions |

The connector normalises documents into `NormalizedDocument` with **tenant-scoped ids** (`id = f"{tenant_id}_{source_id}"`), surfaces every user operation as a standalone `async def` (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::SignWellHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv. SignWell publishes no first-party async Python SDK we'd want to take a hard dep on — direct REST is leaner. |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed. |
| `pydantic` | `>=2.0` | Typed read helpers in `models.py`; pre-installed. |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

SignWell REST API uses **header-based API key authentication** for server-to-server integrations.

### Credentials
- `api_key` — SignWell API key created in **Settings → API → API Keys**. Stored as install_field (type `secret`, required).

### Header contract
Every request to `https://www.signwell.com/api/v1/*`:

```
X-Api-Key:    <api_key>
Accept:       application/json    (or application/pdf on download_completed_document)
Content-Type: application/json    (when a JSON body is present)
```

> **Note:** SignWell uses `X-Api-Key` — **not** `Authorization: Bearer …`. Sending the key in `Authorization` results in 401.

### Lifecycle
- `install()` validates `api_key` is non-empty, persists non-secret config, and materialises a `TokenInfo` (whose `access_token` is the api_key) for surface compatibility with the OAuth path.
- `authorize()` returns the same `TokenInfo` (no exchange — api_key flow has no auth code).
- `health_check()` — `GET /me` as a lightweight account probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Document → NormalizedDocument

| NormalizedDocument | SignWell JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{doc['id']}"` | tenant-scoped |
| `source_id` | `doc["id"]` | SignWell document id |
| `title` | `doc["name"] or doc["subject"]` | |
| `content` | recipient roster + status summary | |
| `source_url` | `doc["files"][0]["url"]` when present | |
| `author` | first recipient `email` | |
| `created_at` | `doc["created_at"]` (RFC 3339 / ISO8601 w/ Z) | |
| `updated_at` | `doc["updated_at"]` | |
| `metadata` | `{status, test_mode, embedded_signing, recipients_count, kind: "signwell.document"}` | |

### 4.2 Template → NormalizedDocument (optional sync surface)

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{tpl['id']}"` |
| `source_id` | `tpl["id"]` |
| `title` | `tpl["name"]` |
| `content` | `tpl.get("description", "")` |
| `metadata` | `{fields_count, kind: "signwell.template"}` |

## 5. Key API Endpoints & Methods

Every public method on `SignWellConnector` is a standalone `async def`. SOC: connector.py orchestrates only; HTTP lives in `client/http_client.py::SignWellHTTPClient`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; materialise TokenInfo. |
| `authorize(...)` | (lifecycle) | n/a | Returns TokenInfo with `access_token=api_key`. |
| `health_check()` | GET | `/me` | Lightweight probe. |
| `sync(...)` | (lifecycle) | iterates `/documents` | Calls `ingest_document` with tenant-scoped ids. |
| `get_me()` | GET | `/me` | Returns the authenticated account payload. |
| `list_documents(page=1, status=None, archived=False, q=None)` | GET | `/documents` | Page-based pagination. |
| `get_document(document_id)` | GET | `/documents/{id}` | |
| `create_document(name, recipients, files=None, file_urls=None, ...)` | POST | `/documents` | Body envelope with optional `files`/`file_urls`. |
| `send_document(document_id)` | POST | `/documents/{id}/send` | Release a draft. |
| `cancel_document(document_id)` | POST | `/documents/{id}/cancel` | |
| `archive_document(document_id)` | POST | `/documents/{id}/archive` | |
| `delete_document(document_id)` | DELETE | `/documents/{id}` | |
| `download_completed_document(document_id, type='completed')` | GET | `/documents/{id}/completed_pdf` | Returns raw PDF bytes. Alias: `download_document`. |
| `list_templates(page=1, q=None)` | GET | `/templates` | |
| `get_template(template_id)` | GET | `/templates/{id}` | |
| `create_document_from_template(template_id, name, recipients, template_fields=None, ...)` | POST | `/document_templates/documents` | |
| `list_recipients(document_id)` | GET | `/documents/{id}/recipients` | |
| `send_reminder(document_id, recipient_id)` | POST | `/documents/{did}/recipients/{rid}/reminder` | |
| `list_webhooks()` | GET | `/api_application/webhooks` | |
| `create_webhook(url, events=None)` | POST | `/api_application/webhooks` | |
| `delete_webhook(webhook_id)` | DELETE | `/api_application/webhooks/{id}` | |

Wire convention: SignWell uses **snake_case** in JSON (`document_id`, `test_mode`, `embedded_signing`, `created_at`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | SignWell meaning | Mapped to |
|---|---|---|
| 400 | Bad request / validation failure | `SignWellBadRequestError` (raise) |
| 401 | API key invalid / missing header | `SignWellAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (account suspended, scope missing) | `SignWellAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Resource not found | `SignWellNotFoundError` (raise) |
| 409 | Conflict (cancelling a completed doc, archiving an archived doc) | `SignWellConflictError` |
| 429 | Rate limited — honours `Retry-After` | `SignWellRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `SignWellServerError` → retry with exponential backoff |
| transport | Timeout / DNS / reset | `SignWellNetworkError` after retries exhausted |

All exceptions live in `exceptions.py` extending `SignWellError`. Retry in `client/http_client.py::_request` honours `_MAX_RETRIES=3`, exponential backoff `0.5s, 1s, 2s` for 429+5xx.

Back-compat aliases preserved:
```
SignWellNotFound = SignWellNotFoundError
```

## 7. Dependencies

Packages declared in `requirements.txt` (read by `install_deps`):

```
httpx>=0.27.0
```

(`structlog`, `pydantic`, `pytest`, `pytest-asyncio`, `pytest-mock`, `respx` are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `X-Api-Key` header value |
| `test_mode_default` | boolean | no | install_field (default `true`) | Default `test_mode` for created documents |
| `base_url` | text | no | install_field (default `https://www.signwell.com/api/v1`) | Sandbox / proxy override |
| `rate_limit_per_min` | number | no | install_field (default 100) | Client-side soft cap |

In `connector.py`:

```python
REQUIRED_CONFIG_KEYS = ["api_key"]
OPTIONAL_CONFIG_KEYS = ["test_mode_default", "base_url", "rate_limit_per_min"]

_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
_SIGNWELL_BASE = "https://www.signwell.com/api/v1"
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers (`X-Api-Key`), retries 429/5xx with exponential backoff, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw SignWell payloads → `NormalizedDocument` with tenant-scoped id. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry`, `safe_get`, signer validation. | `asyncio`, `exceptions` |
| `models.py` | Pydantic typed read helpers (`Recipient`, `TemplateField`, `SignWellDocument`, `CreateDocumentRequest`). | `pydantic` |
| `exceptions.py` | `SignWellError` hierarchy (Auth, BadRequest, NotFound, Conflict, RateLimit, Server, Network). | (stdlib) |
| `__init__.py` | Re-export `SignWellConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, validation) as composable helpers ✓
10. Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only ✓

**Score: 10/10.**
