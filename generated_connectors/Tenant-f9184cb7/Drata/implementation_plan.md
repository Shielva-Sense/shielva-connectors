# Drata Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Drata** is a continuous-compliance automation platform exposing a REST API under `https://public-api.drata.com`. This connector — `DrataConnector` (`CONNECTOR_TYPE = "drata"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Drata workspace:

| Surface | Base path | Capability |
|---|---|---|
| Personnel | `/personnel` | List + read employees / contractors enrolled in the compliance program |
| Controls | `/controls` | List + read controls (SOC 2 / ISO 27001 / HIPAA / PCI-DSS / etc.) |
| Evidence | `/evidence` | List collected evidence items mapped to controls |
| Risks | `/risks` | List risk register entries |
| Vendors | `/vendors` | List + read tracked vendors and their risk posture |
| Audits | `/audits` | List audit instances (SOC 2 Type II, etc.) |
| Compliance Frameworks | `/frameworks` | List frameworks active in the workspace |
| Policies | `/policies` | List policy documents |
| Devices | `/devices` | List enrolled / monitored endpoints |

The connector normalises personnel + controls + evidence + risks + vendors + audits + policies + devices + frameworks into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::DrataHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator for 429 handling |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`, `httpx`, `pydantic`, `structlog`.

## 3. Auth Flow

Drata's public API uses **Bearer-token API key authentication**.

### Credentials
- `api_key` — Drata API key created in **Settings → API Keys → Create**. install_field (type `secret`, required).
- `base_url` — Optional override (defaults to `https://public-api.drata.com`). install_field (type `text`, optional).
- `rate_limit_per_min` — Soft client-side cap. install_field (type `number`, default `60`).

### Header contract
Every request to `https://public-api.drata.com/*`:

```
Authorization: Bearer <api_key>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `api_key` is non-empty. Does **not** call the API.
- `authorize()` — NOT implemented (`api_key` flow has no exchange).
- `health_check()` — `GET /personnel?limit=1` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Personnel → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{person['id']}"` |
| `source_id` | `person["id"]` |
| `title` | `f"{firstName} {lastName}"` |
| `content` | concat name + email + role + employment status |
| `created_at` | `person["createdAt"]` |
| `updated_at` | `person["updatedAt"]` |
| `metadata` | `{status, role, employmentType, email, kind: "drata.personnel"}` |

### 4.2 Control → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{control['id']}"` |
| `source_id` | `control["id"]` |
| `title` | `control["name"]` |
| `content` | `control["description"]` |
| `metadata` | `{status, frameworkIds, owner, kind: "drata.control"}` |

### 4.3 Evidence / Risk / Vendor / Audit / Policy / Device → NormalizedDocument
Same pattern — `id = f"{tenant_id}_{source_id}"`, `metadata.kind = "drata.<resource>"`.

## 5. Key API Endpoints & Methods

Every method listed below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path |
|---|---|---|
| `install()` | (lifecycle) | n/a |
| `health_check()` | GET | `/personnel?limit=1` |
| `sync(since, full, kb_id)` | (lifecycle) | iterates personnel + controls + evidence + risks + vendors |
| `list_personnel(*, limit=100, offset=0, status=None)` | GET | `/personnel` |
| `get_personnel(personnel_id)` | GET | `/personnel/{id}` |
| `list_controls(*, limit=100, offset=0)` | GET | `/controls` |
| `get_control(control_id)` | GET | `/controls/{id}` |
| `list_evidence(*, limit=100, offset=0, control_id=None)` | GET | `/evidence` |
| `list_risks(*, limit=100, offset=0)` | GET | `/risks` |
| `list_vendors(*, limit=100, offset=0)` | GET | `/vendors` |
| `get_vendor(vendor_id)` | GET | `/vendors/{id}` |
| `list_audits(*, limit=100, offset=0)` | GET | `/audits` |
| `list_policies(*, limit=100, offset=0)` | GET | `/policies` |
| `list_devices(*, limit=100, offset=0)` | GET | `/devices` |
| `list_frameworks()` | GET | `/frameworks` |

Wire convention: Drata uses **camelCase** in JSON (`firstName`, `frameworkIds`, `createdAt`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Drata meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `DrataBadRequestError` (raise) |
| 401 | API key invalid | `DrataAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (key lacks scope) | `DrataAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `DrataNotFound` (raise) |
| 429 | Rate limited | `DrataRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `DrataServerError` → retry with exponential backoff |

All in `exceptions.py` extending `DrataError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `0.5 * 2 ** attempt` for 5xx/429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `Authorization: Bearer <api_key>` |
| `base_url` | text | no | install_field (default `https://public-api.drata.com`) | Override for sandbox |
| `rate_limit_per_min` | number | no | install_field (default `60`) | Soft cap |

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
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Drata payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Pagination + retry helpers. | (stdlib only) |
| `models.py` | Pydantic schemas with camelCase aliases. | `pydantic` |
| `exceptions.py` | `DrataError` hierarchy. | (stdlib) |
| `__init__.py` | sys.path self-bootstrap + re-export `DrataConnector`. | `os`, `sys` |

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
