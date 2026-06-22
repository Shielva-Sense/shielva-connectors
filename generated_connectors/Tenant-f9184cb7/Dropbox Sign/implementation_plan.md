# Dropbox Sign Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Dropbox Sign** (formerly **HelloSign**) is a transactional e-signature platform exposing a REST API under `https://api.hellosign.com/v3`. This connector — `DropboxSignConnector` (`CONNECTOR_TYPE = "dropbox_sign"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs:

| Surface | Base path | Capability |
|---|---|---|
| Account | `/account` | Authenticated account metadata + quotas |
| Signature Requests | `/signature_request/*` | Send, list, get, cancel, remind, download, embedded |
| Templates | `/template/*` | List, get, send-with-template |
| Team | `/team` | List team members |
| Unclaimed Drafts | `/unclaimed_draft/list` | List drafts awaiting claim |
| API Apps | `/api_app` | Register an API App (required for embedded signing) |

The connector normalises signature requests + templates into `NormalizedDocument` (`id = f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::DropboxSignHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27.0` | Async client; pre-installed in shared venv. The provider has no first-party Python SDK we'd want to take a hard dep on (the official `dropbox-sign` SDK pulls a lot of synchronous machinery — direct REST is leaner). |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT. |
| `pydantic` | `>=2.0` | Typed read helpers in `models.py`. |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Dropbox Sign uses **HTTP Basic** authentication where the API key is the username and the password is empty.

### Credentials
- `api_key` — Dropbox Sign API key created in **Settings → API → API Keys**. Install field (type `secret`, required).
- `client_id` — optional default API App Client ID for embedded signing. Install field (type `text`, optional).

### Header contract
Every request to `https://api.hellosign.com/v3/*`:

```
Authorization: Basic base64(api_key:)
Accept:        application/json    (or application/pdf / application/zip on download_files)
User-Agent:    shielva-dropbox-sign-connector/1.0
```

POST bodies are `application/x-www-form-urlencoded` with bracket-indexed notation (`signers[0][email_address]=…`, `file_url[0]=…`, `metadata[custom]=…`). File uploads switch to `multipart/form-data` with `file[0]`, `file[1]`, … parts.

### Lifecycle
- `install()` validates `api_key` is non-empty, persists non-secret config, and probes `GET /account`. Returns `HEALTHY + CONNECTED` on 2xx, `OFFLINE + FAILED` on 401/403.
- `authorize()` returns a `TokenInfo` with `access_token=api_key` (surface compatibility — no exchange).
- `health_check()` — `GET /account` as a lightweight probe.
- `ensure_token()` — N/A.

## 4. Data Model

### 4.1 SignatureRequest → NormalizedDocument

| NormalizedDocument | Dropbox Sign JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{sr['signature_request_id']}"` | tenant-scoped |
| `source_id` | `sr["signature_request_id"]` | |
| `title` | `sr["title"] or sr["subject"]` | |
| `content` | subject + message + signer summary | |
| `source_url` | `sr["details_url"] or sr["signing_url"]` | |
| `author` | `sr["requester_email_address"]` | |
| `created_at` | `sr["created_at"]` (epoch seconds) | |
| `metadata` | `{is_complete, is_declined, has_error, requester_email_address, signing_url, details_url, signatures, kind: "dropbox_sign.signature_request"}` | |

### 4.2 Template → NormalizedDocument

| NormalizedDocument | Dropbox Sign JSON |
|---|---|
| `id` | `f"{tenant_id}_{tpl['template_id']}"` |
| `source_id` | `tpl["template_id"]` |
| `title` | `tpl["title"]` |
| `content` | `tpl["message"]` |
| `metadata` | `{can_edit, is_locked, signer_roles, kind: "dropbox_sign.template"}` |

## 5. Key API Endpoints & Methods

Every public method on `DropboxSignConnector` is a standalone `async def`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; probe `/account`. |
| `authorize(...)` | (lifecycle) | n/a | Returns TokenInfo with `access_token=api_key`. |
| `health_check()` | GET | `/account` | Lightweight probe. |
| `sync(...)` | (lifecycle) | iterates `/signature_request/list` | Calls `ingest_document` with tenant-scoped ids. |
| `get_account()` | GET | `/account` | Returns full account payload. |
| `list_signature_requests(page=1, page_size=20, query=None)` | GET | `/signature_request/list` | Page+query params. |
| `get_signature_request(id)` | GET | `/signature_request/{id}` | |
| `send_signature_request(title, subject, message, signers, file_urls=None, files=None, test_mode=None)` | POST | `/signature_request/send` | Form-encoded; multipart when `files` provided. |
| `cancel_signature_request(id)` | POST | `/signature_request/cancel/{id}` | |
| `remind_signature_request(id, email_address)` | POST | `/signature_request/remind/{id}` | |
| `download_files(id, file_type='pdf')` | GET | `/signature_request/files/{id}?file_type={pdf|zip}` | Returns raw bytes. `download_signature_request` alias kept for back-compat. |
| `list_templates(page=1, page_size=20)` | GET | `/template/list` | |
| `get_template(template_id)` | GET | `/template/{id}` | |
| `send_with_template(template_id, title, subject, message, signers, custom_fields=None, test_mode=None)` | POST | `/signature_request/send_with_template` | |
| `list_team_members(page=1)` | GET | `/team` | |
| `list_unclaimed_drafts()` | GET | `/unclaimed_draft/list` | |
| `create_embedded_signature_request(client_id, title, signers, file_urls=None, test_mode=None)` | POST | `/signature_request/create_embedded` | Requires API App `client_id`. |
| `create_api_app(name, domain, callback_url=None)` | POST | `/api_app` | Register an API App for embedded signing. |

Wire convention: Dropbox Sign uses **snake_case** in both query params and the form-encoded body. The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Dropbox Sign meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `DropboxSignBadRequestError` (raise) |
| 401 | API key invalid / missing | `DropboxSignAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (account suspended, key disabled) | `DropboxSignAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `DropboxSignNotFoundError` (raise) |
| 409 | Conflict (e.g. cancelling a completed request) | `DropboxSignConflictError` |
| 429 | Rate limited (per-minute cap) | `DropboxSignRateLimitError` → `ConnectorHealth.DEGRADED`; honours `Retry-After` |
| 5xx | Provider outage | `DropboxSignServerError` → retry with exponential backoff |
| transport | Timeout / DNS / reset | `DropboxSignNetworkError` after retries exhausted |

All exceptions live in `exceptions.py` extending `DropboxSignError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `0.5s, 1s, 2s` for 429+5xx.

## 7. Dependencies

Packages to install in the connector's venv (`install_deps` reads this section):

```
httpx>=0.27.0
structlog>=24.1
pydantic>=2.0
```

(`pytest`, `pytest-asyncio`, `pytest-mock`, `respx` are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | HTTP Basic username |
| `client_id` | text | no | install_field | Default API App Client ID for embedded signing |
| `test_mode_default` | boolean | no | install_field (default `true`) | Default `test_mode` for signature requests |
| `base_url` | text | no | install_field (default `https://api.hellosign.com/v3`) | Sandbox / proxy override |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:

```python
REQUIRED_CONFIG_KEYS = ["api_key"]
OPTIONAL_CONFIG_KEYS = ["client_id", "test_mode_default", "base_url", "rate_limit_per_min"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers (HTTP Basic), retries, raises typed exceptions on HTTP error. Form-encodes nested bracket notation. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Dropbox Sign payloads → `NormalizedDocument` with tenant-scoped id. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry`, `validate_signers`, `safe_get`. | `asyncio`, `exceptions` |
| `models.py` | Pydantic typed read helpers (`Signer`, `Signature`, `SignatureRequest`, `Template`, `ListInfo`). | `pydantic` |
| `exceptions.py` | `DropboxSignError` hierarchy (Auth, BadRequest, NotFound, Conflict, RateLimit, Server, Network). | (stdlib) |
| `__init__.py` | Re-export `DropboxSignConnector`. | `connector` |

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
