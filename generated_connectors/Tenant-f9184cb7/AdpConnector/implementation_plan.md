# ADP Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**ADP** is an enterprise HCM / Payroll / Time / Benefits / Talent suite. ADP Marketplace exposes a REST API under `https://api.adp.com`. This connector — `AdpConnector` (`CONNECTOR_TYPE = "adp"`, `AUTH_TYPE = "oauth2_client_credentials"`) — wraps the operational surfaces a Shielva tenant typically needs from an ADP practitioner account:

| Surface | Base path | Capability |
|---|---|---|
| Workers (HCM) | `/hr/v2/workers` | List + read workers (associates), employees |
| Payroll | `/payroll/v1/workers/{aoid}/pay-statements` + `/payroll/v1/workers/{aoid}/pay-distributions` | List pay statements, fetch a single statement output, list direct-deposit accounts |
| Time | `/time/v2/workers/{aoid}/time-cards` + `/time-off/v2/workers/{aoid}/time-off-requests` | List time cards, time-off requests, submit a new TOR |
| Benefits | `/benefits/v1/workers/{aoid}/enrollments` | List a worker's benefit enrollments |
| Talent | `/hr/v2/jobs` + `/core/v1/organization-units` | List jobs, list org units |
| Webhooks | provider-pushed Events API | (Phase 2 — not in this build) |

The connector normalises workers, pay statements and time-off requests into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), and routes every outbound call through `client/http_client.py::ADPHTTPClient` which owns the OAuth2 token + mTLS handshake.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client with native client-cert (mTLS) support; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`. There is no `tenacity` dependency — exponential backoff is hand-rolled in `_request` to keep the retry semantics tied to ADP's `Retry-After` header handling.

## 3. Auth Flow

ADP Marketplace requires **two layers of credentials simultaneously**:

1. **OAuth 2.0 Client Credentials grant** (machine-to-machine) — `POST /auth/oauth/v2/token`.
2. **Mutual TLS (mTLS)** — every TLS handshake (including the token mint) must present a client certificate + private key issued by ADP when the consumer application is registered.

### Credentials (install_fields)

| Key | Type | Required | Notes |
|---|---|---|---|
| `client_id` | `text` (secret-classified) | yes | OAuth2 client id from ADP Marketplace → My Apps → Credentials |
| `client_secret` | `secret` | yes | OAuth2 client secret from the same screen |
| `client_cert` | `textarea` | yes | PEM-encoded **certificate chain** (begins `-----BEGIN CERTIFICATE-----`). Stored as the literal PEM string; the connector writes it to a per-connector tmp file at install/use time so httpx can load it. |
| `client_key` | `textarea` | yes | PEM-encoded **private key** (begins `-----BEGIN PRIVATE KEY-----` or `-----BEGIN RSA PRIVATE KEY-----`). Same on-disk projection as `client_cert`. |
| `base_url` | `text` | no (default `https://api.adp.com`) | Override for ADP partner sandboxes |
| `token_url` | `text` | no (default `https://accounts.adp.com/auth/oauth/v2/token`) | Override for ADP partner sandboxes |
| `rate_limit_per_min` | `number` | no (default 60) | Client-side soft cap |

### Token-mint request shape

```
POST {token_url}
Content-Type: application/x-www-form-urlencoded
Accept:       application/json

grant_type=client_credentials&client_id={CID}&client_secret={CSEC}
```

The HTTPS connection presents the client cert/key in the TLS handshake. ADP returns:

```json
{"access_token": "...", "expires_in": 3600, "token_type": "Bearer", "scope": "..."}
```

### Resource-call header contract

```
Authorization: Bearer <access_token>
Content-Type:  application/json
Accept:        application/json
```

The TLS connection again presents the client cert/key. Both the token endpoint **and** every resource endpoint require mTLS — there is no "token mint over mTLS, then call without mTLS" pattern.

### Lifecycle

| Phase | Behaviour |
|---|---|
| `install()` | Validate required install_fields are non-empty + PEM-looking. Project `client_cert` + `client_key` to a per-connector tmp file pair so httpx can load them. Does **not** call the API. |
| `authorize(auth_code, state)` | No-op for client-credentials — delegates to `authenticate()`. |
| `authenticate()` | Mint a fresh access token via the client-credentials grant over mTLS. |
| `health_check()` | `GET /hr/v2/workers?$top=1` — lightweight probe; also exercises both the token cache and the mTLS handshake. |
| `ensure_token()` / on-401 | `ADPHTTPClient._request` retries once on 401 after invalidating the cached token. |

## 4. Data Model

### 4.1 Worker → NormalizedDocument

| NormalizedDocument | ADP JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{associateOID}"` | tenant-scoped |
| `source_id` | `worker["associateOID"]` | |
| `title` | `worker["person"]["legalName"]["formattedName"]` | |
| `content` | concat name + job title + status + work-location | |
| `source` | `"adp.workers"` | |
| `created_at` | first `workAssignments[].hireDate` | parsed RFC 3339 |
| `metadata` | `{status, job_title, hire_date, work_email, work_phone}` | |

### 4.2 Pay Statement → NormalizedDocument

| NormalizedDocument | ADP JSON |
|---|---|
| `id` | `f"{tenant_id}_{payStatementID}"` |
| `source_id` | `payStatementID` |
| `title` | `f"Pay statement {payDate}"` |
| `content` | summary string: gross + net + period |
| `source` | `"adp.pay_statements"` |
| `created_at` | `payDate` |
| `metadata` | `{net_pay, currency, pay_period, statement_status}` |

### 4.3 Time-Off Request → NormalizedDocument

| NormalizedDocument | ADP JSON |
|---|---|
| `id` | `f"{tenant_id}_{timeOffRequestID}"` |
| `source_id` | `timeOffRequestID` |
| `title` | `f"Time off {start}–{end}"` |
| `content` | `{policy_code} {hours} ({status})` |
| `source` | `"adp.time_off_requests"` |
| `metadata` | `{policy_code, status, hours}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; project PEM to tmp; init HTTP client. |
| `authenticate()` | POST | `/auth/oauth/v2/token` | Client-credentials grant over mTLS. |
| `health_check()` | GET | `/hr/v2/workers?$top=1` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates workers + pay statements + time-off | Calls `ingest_document`. |
| `list_workers(top=100, skip=0, filter=None, select=None)` | GET | `/hr/v2/workers` | OData paging + filter + select. |
| `get_worker(aoid)` | GET | `/hr/v2/workers/{aoid}` | |
| `list_payments(worker_aoid, top=50, filter=None)` | GET | `/payroll/v1/workers/{aoid}/pay-statements` | OData. |
| `get_payment_outputs(worker_aoid, pay_statement_id)` | GET | `/payroll/v1/workers/{aoid}/pay-statements/{id}` | |
| `list_time_cards(worker_aoid, top=50, filter=None)` | GET | `/time/v2/workers/{aoid}/time-cards` | OData. |
| `list_benefits(worker_aoid, top=50)` | GET | `/benefits/v1/workers/{aoid}/enrollments` | |
| `list_jobs(top=100, filter=None)` | GET | `/hr/v2/jobs` | |
| `list_pay_distributions(worker_aoid)` | GET | `/payroll/v1/workers/{aoid}/pay-distributions` | |
| `list_time_off_requests(worker_aoid, top=50)` | GET | `/time-off/v2/workers/{aoid}/time-off-requests` | |
| `submit_time_off_request(worker_aoid, policy_code, start_date, end_date, hours=None, comments=None)` | POST | `/time-off/v2/workers/{aoid}/time-off-requests` | Events envelope. |
| `list_business_communications(worker_aoid)` | GET | `/hr/v2/workers/{aoid}/business-communications` | |
| `update_personal_communications(worker_aoid, email=None, phone=None)` | POST | `/events/hr/v1/worker.business-communication.email.change` | Events envelope. |
| `list_organizational_units(top=100)` | GET | `/core/v1/organization-units` | |

Wire convention: ADP uses **camelCase** in JSON (`associateOID`, `payStatementID`, `workAssignments`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | ADP meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `ADPBadRequestError` (raise) |
| 401 | Bearer expired / invalid | `ADPAuthError` → invalidate token + retry once → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` |
| 403 | Forbidden (consumer app not entitled) | `ADPAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `ADPNotFound` (raise) |
| 409 | Conflict (e.g. duplicate TOR) | `ADPConflictError` |
| 429 | Rate limited (ADP returns `Retry-After`) | `ADPRateLimitError` (retry, honour header) |
| 5xx | Provider outage | `ADPServerError` → retry with exponential backoff |
| TLS / connect failure | mTLS cert missing or rejected | `ADPNetworkError` |

All in `exceptions.py` extending `ADPError`. Retry in `client/http_client.py::_request` honours `_MAX_RETRIES=3`, exponential backoff `_BACKOFF_BASE_S * 2 ** (attempt-1)` for 5xx, honours `Retry-After` for 429.

## 7. Dependencies

Packages installed in the connector's venv (`install_deps` reads this section):

```
httpx>=0.27,<1.0
structlog>=24.1
```

(`pytest`, `pytest-asyncio`, `pytest-mock`, `respx` are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | text | yes | install_field | OAuth2 client id (logged-safe identifier only) |
| `client_secret` | secret | yes | install_field | OAuth2 client secret |
| `client_cert` | textarea | yes | install_field | PEM cert chain — written to a per-connector tmp file |
| `client_key` | textarea | yes | install_field | PEM private key — written to a per-connector tmp file |
| `base_url` | text | no | install_field (default `https://api.adp.com`) | Provider host |
| `token_url` | text | no | install_field (default `https://accounts.adp.com/auth/oauth/v2/token`) | Token endpoint |
| `rate_limit_per_min` | number | no | install_field (default 60) | Soft cap |

In `connector.py`:

```python
REQUIRED_CONFIG_KEYS = ["client_id", "client_secret", "client_cert", "client_key"]
_STATUS_MAP = {
    401: ("DEGRADED",  "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx + mTLS + OAuth2 client-credentials grant + retry + error mapping. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw ADP payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Event-envelope builders, PEM-to-tempfile projection, OData filter helpers. | (stdlib only) |
| `models.py` | Pydantic / dataclass schemas for request bodies + lightweight refs. | `pydantic` / stdlib `dataclasses` |
| `exceptions.py` | `ADPError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `AdpConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities (PEM projection, OData) in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (mTLS, retry, token cache) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
