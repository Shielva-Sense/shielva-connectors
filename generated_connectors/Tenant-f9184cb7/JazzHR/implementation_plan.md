# JazzHR Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**JazzHR** (internally branded **Resumator**) is a small-business applicant-tracking system (ATS). Its REST API lives at `https://api.resumatorapi.com/v1`. This connector — `JazzHRConnector` (`CONNECTOR_TYPE = "jazzhr"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a JazzHR account:

| Surface | Base path | Capability |
|---|---|---|
| Users | `/users` | Recruiters + hiring managers |
| Jobs | `/jobs` | Job postings (list / get / create) |
| Applicants | `/applicants` | Candidate records (list / get / create / search) |
| Applicant↔Job | `/applicants2jobs` | Attach applicant to job |
| Notes | `/notes` | Internal notes on applicants |
| Activities | `/activities` | Audit-trail events on applicants |
| Rating | `/ratings` | Hiring-team rating steps |
| Categories | `/categories` | Workflow categories (e.g. Sales, Engineering) |
| Workflows | `/workflows` | Workflow stages (Phone Screen, Onsite, Offer) |
| Contacts | `/contacts` | External contacts (referrers, vendors) |
| Tasks | `/tasks` | Recruiter to-do items |

The connector normalises jobs + applicants + notes into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"` — per CONNECTOR_SYSTEM_PROMPT), surfaces standalone `async def` methods per user-requested operation (OCP), pages through every list endpoint via the JazzHR `?page=N` cursor, retries `429` / `5xx` with jittered exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::JazzHRHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per `CONNECTOR_SYSTEM_PROMPT`; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `respx`.

JazzHR uses neither OAuth nor JWT — there is no signing library to add.

## 3. Auth Flow

JazzHR REST API uses **API key authentication carried as a query parameter** (the provider's documented quirk).

### Credentials
- `api_key` — generated in **JazzHR → Settings → Integrations → API**. Stored as install_field (type `secret`, required). Sent on **every** request as `?apikey=<api_key>`. **Never** sent in `Authorization`, `X-Api-Key`, or any other header.
- `base_url` — optional override of `https://api.resumatorapi.com/v1` (sandbox / proxy / regional). install_field (type `string`, optional).
- `rate_limit_per_min` — client-side soft cap (default `60`). install_field (type `number`, optional).
- `default_user_id` — JazzHR user UUID used as the author of `add_note(...)` when the caller omits `user_id`. install_field (type `string`, optional).

### Wire contract
Every request:

```
GET  /<resource>?apikey=<api_key>&<other params>
POST /<resource>?apikey=<api_key>     (body: application/x-www-form-urlencoded)
```

POST bodies are form-encoded — JazzHR rejects `application/json` on its mutation endpoints.

### Lifecycle
- `install()` validates `api_key` is non-empty, persists config, and probes `GET /jobs?page=1` to verify the key. 401/403 → `MISSING_CREDENTIALS`.
- `authorize()` — NO-OP (returns a `TokenInfo` whose `access_token` is the api_key, for ABI uniformity).
- `health_check()` — `GET /jobs?page=1` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Job → NormalizedDocument

| NormalizedDocument | JazzHR JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{job['id']}"` | tenant-scoped per spec |
| `source_id` | `job["id"]` | JazzHR job UUID |
| `title` | `job["title"]` | |
| `content` | `job["description"]` | HTML-detected via `<` heuristic |
| `source` | `"jazzhr"` | |
| `author` | `job["hiring_lead"]` | |
| `created_at` | `job["original_open_date"]` | `YYYY-MM-DD HH:MM:SS` |
| `updated_at` | `job["updated_at"]` | |
| `metadata` | `{kind:"job", status, department, city, state, country_id, type, board_code}` | |

### 4.2 Applicant → NormalizedDocument

| NormalizedDocument | JazzHR JSON |
|---|---|
| `id` | `f"{tenant_id}_{app['id']}"` |
| `source_id` | `app["id"]` |
| `title` | `f"{first_name} {last_name}"` |
| `content` | `app["cover_letter"]` or `app["description"]` |
| `source` | `"jazzhr"` |
| `author` | `app["email"]` |
| `created_at` | `app["apply_date"]` |

### 4.3 Note → NormalizedDocument

| NormalizedDocument | JazzHR JSON |
|---|---|
| `id` | `f"{tenant_id}_{note['id']}"` |
| `source_id` | `note["id"]` |
| `title` | `f"Note on applicant {applicant_id}"` |
| `content` | `note["contents"]` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | probes `/jobs?page=1` | Validates key, persists config |
| `health_check()` | GET | `/jobs?page=1` | Lightweight probe |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates `/jobs` + `/applicants` | Calls `ingest_document` per row |
| `list_users(page=1)` | GET | `/users?page=N` | |
| `get_user(user_id)` | GET | `/users/{id}` | |
| `list_jobs(page=1, status=None, title=None, ...)` | GET | `/jobs?page=N&status=...` | |
| `get_job(job_id)` | GET | `/jobs/{id}` | |
| `create_job(title, hiring_lead_id, type, ...)` | POST | `/jobs` | form-encoded |
| `list_applicants(page=1, status=None, name=None, ...)` | GET | `/applicants?page=N` | |
| `get_applicant(applicant_id)` | GET | `/applicants/{id}` | |
| `create_applicant(first_name, last_name, email, ...)` | POST | `/applicants` | form-encoded |
| `assign_applicant_to_job(applicant_id, job_id)` | POST | `/applicants2jobs` | form-encoded |
| `list_applicants_by_job(job_id, page=1)` | GET | `/applicants/job_id/{id}?page=N` | |
| `list_notes(applicant_id, page=1)` | GET | `/notes/applicant_id/{id}?page=N` | |
| `add_note(applicant_id, contents, security, user_id)` | POST | `/notes` | form-encoded; falls back to `default_user_id` |
| `list_activities(applicant_id=None, page=1)` | GET | `/activities[?applicant_id=...]` | |
| `list_rating_steps()` | GET | `/ratings` | |
| `list_workflows()` | GET | `/categories` | JazzHR's "categories" == workflow buckets |
| `list_workflow_steps()` | GET | `/workflows` | Stages within a workflow |
| `list_categories()` | GET | `/categories` | Alias of list_workflows (semantic surface) |
| `list_contacts(page=1)` | GET | `/contacts?page=N` | |
| `list_tasks(page=1)` | GET | `/tasks?page=N` | |

Wire convention: JazzHR uses **snake_case** in JSON (`first_name`, `apply_date`, `board_code`); the connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | JazzHR meaning | Mapped to |
|---|---|---|
| 400 | Bad request / malformed form body | `JazzHRError` (raise) |
| 401 | API key invalid | `JazzHRAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden / key revoked | `JazzHRAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `JazzHRNotFound` (raise) |
| 429 | Rate limited (Retry-After respected on first retry) | retried; raises `JazzHRNetworkError` after `_MAX_RETRIES` |
| 5xx | Provider outage | retried; raises `JazzHRNetworkError` after `_MAX_RETRIES` |

All exceptions in `exceptions.py` extending `JazzHRError`. Retry in `client/http_client.py::_request` honours `_MAX_RETRIES = 3` with `RETRY_DELAY_S * (BACKOFF_FACTOR ** attempt) + jitter` capped at `MAX_RETRY_DELAY_S = 32s`.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# (none — httpx, structlog, pytest, pytest-asyncio, respx are pre-installed in the shared venv)
```

`requirements.txt` lists only what is JazzHR-specific (currently nothing beyond the pre-installed shared set; the file pins the minimums explicitly for portability).

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | Sent as `?apikey=` query param |
| `base_url` | string | no | install_field (default `https://api.resumatorapi.com/v1`) | Sandbox / regional override |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |
| `default_user_id` | string | no | install_field | Fallback author for `add_note` |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key"]
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
| `client/http_client.py` | Single owner of httpx. Injects `?apikey=`, form-encodes POST bodies, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw JazzHR payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | List coercion helper (`ensure_list`) + date parsing. | (stdlib only) |
| `models.py` | Local dataclasses for Job / Applicant / Note / WorkflowStep shape hints. | (stdlib) |
| `exceptions.py` | `JazzHRError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `JazzHRConnector`. | `connector` |

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
