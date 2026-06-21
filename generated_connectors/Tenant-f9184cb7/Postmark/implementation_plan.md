# Postmark Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Postmark** is a transactional + broadcast email delivery platform exposing a REST API at `https://api.postmarkapp.com`. This connector — `PostmarkConnector` (`CONNECTOR_TYPE = "postmark"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Postmark account:

| Surface | Base path | Token kind | Capability |
|---|---|---|---|
| Email send | `/email`, `/email/batch`, `/email/withTemplate` | server | Single send, batch send (≤500), templated send |
| Server info | `/server` | server | Server settings + health probe |
| Outbound messages | `/messages/outbound`, `/messages/outbound/{id}/details` | server | Paginated search + per-message detail |
| Inbound messages | `/messages/inbound` | server | Inbound message stream listing |
| Bounces | `/bounces`, `/bounces/{id}`, `/bounces/{id}/activate` | server | List, fetch, reactivate suppressed recipient |
| Templates | `/templates`, `/templates/{id_or_alias}` | server | Catalogue + content fetch |
| Stats | `/stats/outbound` | server | Aggregated outbound delivery stats |
| Servers | `/servers` | **account** | Account-wide server registry |
| Domains | `/domains` | **account** | Sender-domain registry + DKIM/SPF status |

The connector normalises outbound messages into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::PostmarkHTTPClient`).

### Server token vs Account token split

Postmark has **two distinct credentials** that are NOT interchangeable:

- **Server token** (`X-Postmark-Server-Token` header) — scoped to a single Postmark "server" (their term for a configured sending stream). Required for every send + every per-server query: `/email/*`, `/server`, `/messages/*`, `/bounces`, `/templates`, `/stats/*`.
- **Account token** (`X-Postmark-Account-Token` header) — scoped to the Postmark account. Required only for account-wide registries: `/servers`, `/domains`, `/senders`.

The connector takes `server_token` as **required** (most surfaces need it) and `account_token` as **optional** (only consumed by `list_servers` / `list_domains`). The HTTP client picks the header by `endpoint_kind` ("server" or "account") so the wrong credential is never sent — and a missing account_token at call time surfaces as a typed `PostmarkAuthError` instead of a 401 round-trip.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

No JWT library required — Postmark webhook signatures are HMAC-SHA256 (not JWT) and webhook verification lives downstream of the connector boundary in Shielva-side ingress, not in this module.

## 3. Auth Flow

Postmark REST API uses **server-to-server API token authentication** (`AUTH_TYPE = "api_key"`). No OAuth dance, no token refresh, no expiry.

### Credentials
- `server_token` — Postmark **server** token created in **Servers → your server → API Tokens**. install_field (type `secret`, **required**). Sent as `X-Postmark-Server-Token`.
- `account_token` — Postmark **account** token created in **Account → API Tokens**. install_field (type `secret`, optional). Sent as `X-Postmark-Account-Token` for account-scoped surfaces only.
- `default_from_email` — verified sender address used when `send_email`/`send_with_template` callers omit the `from_email` arg. install_field (type `string`, optional).

### Header contract

Every server-scoped request to `https://api.postmarkapp.com/*`:

```
X-Postmark-Server-Token: <server_token>
Accept:                  application/json
Content-Type:            application/json
```

Every account-scoped request:

```
X-Postmark-Account-Token: <account_token>
Accept:                   application/json
Content-Type:             application/json
```

### Lifecycle
- `install()` validates `server_token` is non-empty, then probes `GET /server` to confirm the token is real. 401 → `MISSING_CREDENTIALS`. Network errors → `DEGRADED but installed`.
- `authorize()` — no-op for api_key auth — returns an empty `TokenInfo(token_type="api_key")` to satisfy the `BaseConnector` ABI.
- `health_check()` — `GET /server` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Outbound Message → NormalizedDocument

| NormalizedDocument | Postmark JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{MessageID}"` | tenant-scoped |
| `source_id` | `MessageID` | Postmark message UUID |
| `title` | `Subject` (falls back to `"(no subject)"`) | |
| `content` | `TextBody` ∥ stripped(`HtmlBody`) ∥ `Subject` | best-effort plain text |
| `content_type` | `"text"` | |
| `source_url` | `https://account.postmarkapp.com/servers/messages/{MessageID}` | direct deeplink to Postmark UI |
| `author` | `From` ∥ `FromEmail` | |
| `created_at` | parsed `ReceivedAt` ∥ `SubmittedAt` (UTC) | |
| `metadata` | `{to, cc, bcc, tag, status, message_stream, html_body, events}` | |

### 4.2 Inbound Message → NormalizedDocument

Same shape as 4.1, but `source_url` points at `/messages/inbound/{id}` and metadata adds `mailbox_hash`, `original_recipient`.

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Token | Notes |
|---|---|---|---|---|
| `install()` | (lifecycle) | n/a | n/a | Validate config; probe `/server`. |
| `health_check()` | GET | `/server` | server | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates `/messages/outbound` | server | Calls `ingest_document` per message. |
| `authorize(code, state)` | (lifecycle) | n/a | n/a | No-op TokenInfo. |
| `get_server()` | GET | `/server` | server | Server settings. |
| `send_email(from_email, to, subject, html_body, text_body, cc, bcc, tag, metadata, message_stream)` | POST | `/email` | server | Single transactional send. |
| `send_email_batch(messages)` | POST | `/email/batch` | server | Up to 500 messages per call. |
| `send_email_with_template(template_id, template_alias, from_email, to, template_model, message_stream)` | POST | `/email/withTemplate` | server | Exactly one of template_id or template_alias. |
| `get_message_details(message_id)` | GET | `/messages/outbound/{id}/details` | server | Full envelope + events. |
| `list_messages(count, offset, recipient, from_email, tag, status)` | GET | `/messages/outbound` | server | Paginated outbound search. |
| `list_inbound_messages(count, offset, recipient, from_email, subject, status)` | GET | `/messages/inbound` | server | Paginated inbound search. |
| `list_bounces(count, offset, type, inactive, email_filter)` | GET | `/bounces` | server | Bounce log search. |
| `get_bounce(bounce_id)` | GET | `/bounces/{id}` | server | |
| `activate_bounce(bounce_id)` | PUT | `/bounces/{id}/activate` | server | Reactivate suppressed recipient. |
| `list_templates(count, offset)` | GET | `/templates` | server | Template catalogue. |
| `get_template(template_id_or_alias)` | GET | `/templates/{id_or_alias}` | server | |
| `create_template(name, subject, html_body, text_body, alias)` | POST | `/templates` | server | Provision a new template. |
| `list_servers()` | GET | `/servers` | **account** | Account-wide server registry. |
| `get_server_by_id(server_id)` | GET | `/servers/{id}` | **account** | Per-server detail. |
| `list_domains()` | GET | `/domains` | **account** | Sender-domain registry. |
| `get_stats_overview(tag, from_date, to_date)` | GET | `/stats/outbound` | server | Aggregated delivery stats. |

Wire convention: Postmark uses **PascalCase** in JSON (`MessageID`, `SubmittedAt`, `HtmlBody`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads and exposes Pythonic snake_case at the method signature.

## 6. Error Handling

| HTTP | Postmark meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `PostmarkBadRequestError` (raise) |
| 401 | Token invalid / missing header | `PostmarkAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (token lacks scope) | `PostmarkAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `PostmarkNotFoundError` (raise) |
| 422 + `ErrorCode 406` | Inactive recipient | `PostmarkInactiveRecipient` (raise — caller decides) |
| 422 + `ErrorCode 10` | Invalid token | `PostmarkAuthError` (treated as 401) |
| 422 (other ErrorCodes) | Bad request body | `PostmarkBadRequestError` (raise) |
| 429 | Rate limited (Postmark default cap ~600/min) | `PostmarkRateLimitError` → `ConnectorHealth.DEGRADED`, retried with backoff |
| 5xx | Provider outage | `PostmarkServerError` → retried with exponential backoff |

All in `exceptions.py` extending `PostmarkError`. Retry in `helpers/utils.py::with_retry` honours `max_retries=3` with exponential backoff `_BACKOFF_BASE * 2 ** attempt` (1s, 2s, 4s) for 429 + 5xx + transport errors.

Back-compat aliases preserved: `PostmarkNetworkError = PostmarkServerError`.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27,<1.0
structlog>=24.1
```

(`pydantic`, `pytest`, `pytest-asyncio`, `pytest-mock`, `respx` are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `server_token` | secret | yes | install_field | `X-Postmark-Server-Token` header value |
| `account_token` | secret | no | install_field | `X-Postmark-Account-Token` header value (only for `/servers`, `/domains`) |
| `default_from_email` | string | no | install_field | Used when caller omits `from_email` |
| `base_url` | string | no | install_field (default `https://api.postmarkapp.com`) | Override for Postmark sandbox or proxy |
| `rate_limit_per_min` | number | no | install_field (default 600) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["server_token"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
_POSTMARK_BASE = "https://api.postmarkapp.com"
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers per token kind, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Postmark payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry` (exponential backoff), `safe_get`, ISO date parsing. | (stdlib + `structlog`) |
| `models.py` | Pydantic schemas + dataclass shims for `SendResult`, `ServerInfo`, `Bounce` with PascalCase + snake_case property bridge. | `pydantic`, `shared.base_connector` |
| `exceptions.py` | `PostmarkError` hierarchy + back-compat aliases. | (stdlib) |
| `__init__.py` | Re-export `PostmarkConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only — no httpx import ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, header selection) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches typed exceptions only ✓

**Score: 10/10.**
