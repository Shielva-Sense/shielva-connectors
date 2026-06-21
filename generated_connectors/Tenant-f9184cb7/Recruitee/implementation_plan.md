# Recruitee Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Recruitee** is a collaborative Applicant Tracking System (ATS) that exposes a REST API under `https://api.recruitee.com/c/{company_id}`. This connector — `RecruiteeConnector` (`CONNECTOR_TYPE = "recruitee"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Recruitee workspace:

| Surface | Base path | Capability |
|---|---|---|
| Candidates | `/candidates` | List, get, create, update, delete candidate records |
| Offers (Jobs) | `/offers` | List, get, create job requisitions |
| Departments | `/departments` | List company departments used to scope offers |
| Pipelines | `/pipeline_templates` | List per-company pipeline templates |
| Stages | `/offers/{offer_id}/stages` | List the stages of a specific offer's pipeline |
| Tags | `/tags` | List candidate tags |
| Notes | `/candidates/{id}/notes` | List + create candidate notes |
| Messages | `/candidates/{id}/mailbox` | List candidate email/message threads |
| Tasks | `/tasks` | List company tasks (assignable to candidates / offers) |
| Hiring Managers | `/admins` | List hiring managers / admins |
| Current User | `/current_user` | Used as the `health_check` probe |

The connector normalises Candidates and Offers into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts, `Retry-After` honoured), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::RecruiteeHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Recruitee REST API uses **Personal API Token** authentication for server-to-server integrations.

### Credentials
- `company_id` — Recruitee company UUID/integer that appears in the path-segment of every API call (`/c/{company_id}/...`). install_field (type `string`, required).
- `api_token` — Personal API Token generated in **Settings → Apps & Plugins → Personal API Tokens**. Sent as `Authorization: Bearer <token>`. install_field (type `secret`, required).
- `base_url` — Override for the API base. install_field (type `string`, optional, default `https://api.recruitee.com/c`).
- `rate_limit_per_min` — Client-side soft cap. install_field (type `number`, optional, default 60).

### Header contract
Every request to `https://api.recruitee.com/c/{company_id}/*`:

```
Authorization: Bearer <api_token>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `company_id` + `api_token` are non-empty, then probes `GET /current_user` to verify the token resolves and binds to the company. On 2xx → `HEALTHY + CONNECTED`. On 401 → `OFFLINE + FAILED`.
- `authorize()` — NOT implemented (API-key flow has no exchange). Returns an empty `TokenInfo` with `token_type="api_key"` for surface parity.
- `health_check()` — `GET /current_user` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Candidate → NormalizedDocument

| NormalizedDocument | Recruitee JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{candidate['id']}"` | tenant-scoped per system prompt |
| `source_id` | `str(candidate["id"])` | Recruitee numeric ID |
| `title` | `candidate["name"]` | |
| `content` | `candidate["name"]` + emails + phones joined | |
| `source` | `"recruitee.candidates"` | |
| `created_at` | `candidate["created_at"]` | RFC 3339 |
| `updated_at` | `candidate["updated_at"]` | |
| `metadata` | `{emails, phones, source, photo_thumb_url, kind: "recruitee.candidate"}` | |

### 4.2 Offer → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{offer['id']}"` |
| `source_id` | `str(offer["id"])` |
| `title` | `offer["title"]` |
| `content` | strip-tag concat of `description` + `requirements` |
| `source` | `"recruitee.offers"` |
| `created_at` | `offer["created_at"]` |
| `updated_at` | `offer["updated_at"]` |
| `metadata` | `{status, position_type, employment_type_code, department_id, location_ids, kind: "recruitee.offer"}` |

## 5. Key API Endpoints & Methods

Every method listed below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | `GET /current_user` | Validate config + probe token. |
| `health_check()` | GET | `/current_user` | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates candidates + offers | Calls `ingest_document` per item. |
| `list_candidates(limit, offset, query, sort, scope, status)` | GET | `/candidates` | Offset pagination. |
| `get_candidate(candidate_id)` | GET | `/candidates/{id}` | |
| `create_candidate(name, emails, phones, source, offers)` | POST | `/candidates` | Body: `{candidate: {...}, offers: [{id}]}`. |
| `update_candidate(candidate_id, fields)` | PATCH | `/candidates/{id}` | Body: `{candidate: {...}}`. |
| `delete_candidate(candidate_id)` | DELETE | `/candidates/{id}` | |
| `list_offers(limit, offset, status, scope)` | GET | `/offers` | Offset pagination. |
| `get_offer(offer_id)` | GET | `/offers/{id}` | |
| `create_offer(title, position_type, employment_type_code, department_id, location_ids, description_html, requirements_html)` | POST | `/offers` | Body: `{offer: {...}}`. |
| `list_departments()` | GET | `/departments` | |
| `list_pipelines()` | GET | `/pipeline_templates` | |
| `list_stages(offer_id)` | GET | `/offers/{offer_id}/stages` | Per-offer pipeline stages. |
| `list_notes(candidate_id)` | GET | `/candidates/{candidate_id}/notes` | |
| `create_note(candidate_id, body, visible_to_team_id)` | POST | `/candidates/{candidate_id}/notes` | Body: `{note: {...}}`. |
| `list_tasks(limit, offset)` | GET | `/tasks` | |
| `list_tags()` | GET | `/tags` | |
| `list_hiring_managers()` | GET | `/admins` | |

Wire convention: Recruitee uses **snake_case** in JSON (`created_at`, `position_type`, `employment_type_code`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Recruitee meaning | Mapped to |
|---|---|---|
| 400 | Bad request / validation | `RecruiteeError` (raise) |
| 401 | Token invalid / missing | `RecruiteeAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` |
| 403 | Token lacks scope | `RecruiteeAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Resource not found | `RecruiteeNotFound` (raise) |
| 429 | Rate-limited (`Retry-After` header) | `RecruiteeRateLimitError` → backoff & retry up to 3 times; honour `Retry-After` |
| 5xx | Provider outage | `RecruiteeNetworkError` → exponential backoff retry |

All in `exceptions.py` extending `RecruiteeError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt` for 5xx, max of (backoff, `Retry-After` seconds) for 429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27.0
structlog>=24.1
```

(pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `company_id` | string | yes | install_field | Path segment `/c/{company_id}` |
| `api_token` | secret | yes | install_field | `Authorization: Bearer <token>` |
| `base_url` | string | no | install_field (default `https://api.recruitee.com/c`) | Override for proxy / sandbox |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["company_id", "api_token"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers, retries 429/5xx, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Recruitee payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry helper, payload builders (`build_candidate_payload`, `build_offer_payload`, `build_note_payload`, `build_list_query`). | (stdlib only) |
| `models.py` | Pydantic schemas for request/response bodies. | `pydantic` |
| `exceptions.py` | `RecruiteeError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `RecruiteeConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Payload builders in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is a standalone `async def` ✓
7. New surfaces added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, pagination) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
