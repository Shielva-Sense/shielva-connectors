# Vanta Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Vanta** is a compliance-automation SaaS that continuously monitors security controls, vendor risk, personnel access, and audit evidence to maintain SOC 2, ISO 27001, HIPAA, GDPR, and PCI DSS attestations. This connector — `VantaConnector` (`CONNECTOR_TYPE = "vanta"`, `AUTH_TYPE = "oauth2_client_credentials"`) — wraps the operational surfaces a Shielva tenant typically needs from Vanta:

| Surface | Base path | Capability |
|---|---|---|
| Frameworks | `/v1/frameworks` | List + read compliance frameworks (SOC 2, ISO 27001, HIPAA, GDPR, PCI DSS) |
| Controls | `/v1/controls` | List + read controls scoped to a framework |
| Vendors | `/v1/vendors` | Third-party vendor inventory + risk metadata |
| Personnel | `/v1/personnel` | Employees, contractors, role assignments |
| Risks | `/v1/risks` | Risk register entries with likelihood + impact |
| Incidents | `/v1/incidents` | Security incidents with severity + status |
| Documents | `/v1/documents` | Policy / SOP documents under version control |
| Tests | `/v1/tests` | Continuous control tests + status |
| Findings | `/v1/findings` | Open findings flagged by Vanta scanners |
| Audits | `/v1/audits` | Active and historical audit engagements |

The connector normalises frameworks + controls + vendors + personnel into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::VantaHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator for 429 / 5xx handling |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Vanta REST API uses **OAuth 2.0 client_credentials** (machine-to-machine) for server integrations.

### Credentials
- `client_id` — Vanta OAuth client id from **Vanta → Settings → API → Create OAuth App**. install_field (type `string`, required).
- `client_secret` — Paired secret. install_field (type `secret`, required).
- `scopes` — Space-separated list of scopes. install_field (type `string`, optional; default `vanta-api.all:read vanta-api.vendors:write`).
- `base_url` — Override for `https://api.vanta.com/v1`. install_field (type `string`, optional).
- `token_url` — Override for `https://api.vanta.com/oauth/token`. install_field (type `string`, optional).
- `rate_limit_per_min` — Soft cap. install_field (type `number`, optional; default 60).

### Token lifecycle
1. `install()` validates `client_id` + `client_secret` are present and persists config. It does **not** call the API.
2. First request lazily mints an access token via `POST https://api.vanta.com/oauth/token` with `grant_type=client_credentials`, `client_id`, `client_secret`, `scope`.
3. Token cached in-memory (`asyncio.Lock` guarded) with a 60 s leeway before `expires_in`.
4. `401` on a downstream request triggers a single re-mint and one retry — no recursion.
5. `health_check()` issues `GET /v1/frameworks?pageSize=1` after acquiring the token.

### Header contract
Every request to `https://api.vanta.com/v1/*`:

```
Authorization: Bearer <access_token>
Content-Type:  application/json
Accept:        application/json
```

## 4. Data Model

### 4.1 Framework → NormalizedDocument

| NormalizedDocument | Vanta JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{framework['id']}"` | tenant-scoped |
| `source_id` | `framework["id"]` | Vanta framework UUID |
| `title` | `framework["name"]` | e.g. `SOC 2 Type II` |
| `content` | `framework["description"]` | |
| `source` | `"vanta.framework"` | |
| `created_at` | `framework["createdAt"]` | RFC 3339 |
| `updated_at` | `framework["updatedAt"]` | |
| `metadata` | `{slug, status, progress, certificationStatus}` | |

### 4.2 Control → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{control['id']}"` |
| `source_id` | `control["id"]` |
| `title` | `control["name"]` |
| `content` | `control["description"]` |
| `source` | `"vanta.control"` |
| `metadata` | `{frameworkId, controlOwnerId, status, lastTestedAt, severity}` |

### 4.3 Vendor → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{vendor['id']}"` |
| `source_id` | `vendor["id"]` |
| `title` | `vendor["name"]` |
| `content` | `vendor["description"]` |
| `source` | `"vanta.vendor"` |
| `metadata` | `{websiteUrl, ownerEmail, riskLevel, status}` |

### 4.4 Personnel → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{person['id']}"` |
| `source_id` | `person["id"]` |
| `title` | `person.get("displayName") or person["email"]` |
| `content` | concat name + email + role |
| `source` | `"vanta.personnel"` |
| `metadata` | `{email, role, employmentStatus, isActive}` |

## 5. Key API Endpoints & Methods

Every method listed here MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. Does NOT call the API. |
| `health_check()` | GET | `/frameworks?pageSize=1` | Lightweight probe (mints token if absent). |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates frameworks + controls + vendors + personnel | Calls `ingest_document`. |
| `list_frameworks(page_size, page_cursor)` | GET | `/frameworks` | Cursor pagination. |
| `get_framework(framework_id)` | GET | `/frameworks/{framework_id}` | |
| `list_controls(page_size, page_cursor, framework_id)` | GET | `/controls` | Optional framework filter. |
| `get_control(control_id)` | GET | `/controls/{control_id}` | |
| `list_vendors(page_size, page_cursor)` | GET | `/vendors` | |
| `get_vendor(vendor_id)` | GET | `/vendors/{vendor_id}` | |
| `list_personnel(page_size, page_cursor, includes_inactive)` | GET | `/personnel` | |
| `get_personnel(person_id)` | GET | `/personnel/{person_id}` | |
| `list_risks(page_size, page_cursor)` | GET | `/risks` | |
| `list_incidents(page_size, page_cursor, severity, status)` | GET | `/incidents` | |
| `list_documents(page_size, page_cursor)` | GET | `/documents` | |
| `list_tests(page_size, page_cursor, test_status)` | GET | `/tests` | |
| `list_findings(page_size, page_cursor, severity, status)` | GET | `/findings` | |
| `list_audits(page_size, page_cursor)` | GET | `/audits` | |

Wire convention: Vanta uses **camelCase** in JSON (`createdAt`, `frameworkId`, `pageCursor`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Vanta meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `VantaError` (raise) |
| 401 | Access token invalid / expired | `VantaAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` (after single re-mint retry) |
| 403 | Forbidden (scope insufficient) | `VantaAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `VantaNotFound` (raise) |
| 429 | Rate limited (honour `Retry-After`) | `VantaRateLimitError` → retry with backoff (max 3) → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `VantaNetworkError` → retry with exponential backoff (max 3) |

All in `exceptions.py` extending `VantaError`. Retry in `client/http_client.py::_request` honours `MAX_RETRIES=3`, exponential backoff `RETRY_DELAY_S * BACKOFF_FACTOR ** attempt` capped at `MAX_RETRY_DELAY_S=16s`, plus `Retry-After` when present.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | string | yes | install_field | OAuth client id |
| `client_secret` | secret | yes | install_field | OAuth client secret |
| `scopes` | string | no | install_field (default `vanta-api.all:read vanta-api.vendors:write`) | Space-separated scope list |
| `base_url` | string | no | install_field (default `https://api.vanta.com/v1`) | API base URL |
| `token_url` | string | no | install_field (default `https://api.vanta.com/oauth/token`) | OAuth token endpoint |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

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
| `client/http_client.py` | Single owner of httpx. Mints + caches OAuth token. Retries. Raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Vanta payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry helper, ISO date parsing, safe-get. | (stdlib only) |
| `models.py` | Pydantic schemas with camelCase aliases for request bodies. | `pydantic` |
| `exceptions.py` | `VantaError` hierarchy. | (stdlib) |
| `__init__.py` | Self-bootstrap sys.path + re-export `VantaConnector`. | `connector` |

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
