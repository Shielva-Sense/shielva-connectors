# Research & Plan Implementation

## What to do
Research Bandwidth's three first-party APIs — Messaging (https://messaging.bandwidth.com/api/v2), Voice (https://voice.bandwidth.com/api/v2), and Numbers/Dashboard (https://dashboard.bandwidth.com/api). Capture the HTTP Basic auth flow (account_id in URL + username:password header), the camelCase wire format (applicationId, answerUrl, sourceTn, mediaUrl), RFC 5988 Link-header cursor pagination on Messaging+Voice, page/size on Dashboard, Retry-After-honouring 429 behaviour, and the HMAC-SHA256 X-Callback-Signature scheme for webhooks. Produce implementation_plan.md with 9 sections including §7 dependencies for the install_deps step.

## Connector Identity
- **Provider**: bandwidth
- **Service**: bandwidth
- **Auth Type**: basic

## User Requirements
Build a Bandwidth CPaaS connector covering three surfaces with one shared HTTP Basic credential pair:

- Messaging (https://messaging.bandwidth.com/api/v2): send_message, get_message, list_messages (filterable, paginated), list_media, upload_media (PUT binary), delete_media.
- Voice (https://voice.bandwidth.com/api/v2): create_call, get_call, update_call, list_calls (paginated), get_call_recordings, download_recording (raw audio bytes).
- Numbers / Dashboard (https://dashboard.bandwidth.com/api): list_phone_numbers, list_applications.

Auth = HTTP Basic with account_id (numeric, part of every URL path) + API username + password — install_fields, never hardcoded.

Webhook layer: handle_webhook routes Bandwidth event callbacks (message-received, message-delivered, message-failed, bridge-complete, recording-available) to per-event handlers. process_callback verifies the X-Callback-Signature using HMAC-SHA256 with the webhook_secret install_field (timing-safe compare). Also: handle_event for idempotency-keyed acks and batch_processor for batched event delivery.

Wire format: Messaging + Voice are JSON with camelCase field names (applicationId, answerUrl, sourceTn, mediaUrl). Pagination is RFC 5988 Link header cursor (pageToken=) on Messaging + Voice; page/size on Dashboard. Honour Retry-After on 429 and bounded retry on 5xx.

Multi-tenant: every NormalizedDocument id = f"{self.tenant_id}_{source_id}".


## SOC / OCP Compliance Requirements
This connector MUST be built following these non-negotiable architectural principles.
Plan for ALL of them explicitly — the SOC/OCP compliance audit step will score 0–10:

**Separation of Concerns (SOC) — 5 checks:**
1. `connector.py` ONLY orchestrates — zero raw HTTP calls, zero JSON parsing
2. All HTTP calls delegated to `client/http_client.py`
3. All response transformations delegated to `helpers/normalizer.py`
4. All utilities/helpers in `helpers/utils.py`
5. `connector.py` imports from client/ and helpers/ — never reimplements their logic

**Open/Closed Principle (OCP) — 5 checks:**
6. Each user-requested operation is a standalone `async def` method — NOT folded into `sync()`
7. New operations can be added without modifying BaseConnector or existing methods
8. Config values always come from `self.config.get("key")` — no hardcoded credentials/URLs
9. Features (retry, pagination, rate-limiting) implemented as composable helpers, not inline
10. Error mapping delegated to exceptions.py — connector.py catches custom exceptions only

**Score must be 10/10 (100%) for the compliance step to pass.**
Plan your file structure and method separation to achieve this from the start.

## ⚠️ MANDATORY — Methods to implement (every one MUST appear in implementation_plan.md Section 5)
- `install()`
- `health_check()`
- `sync()`
- `send_message()`
- `get_message()`
- `list_messages()`
- `list_media()`
- `upload_media()`
- `delete_media()`
- `create_call()`
- `get_call()`
- `update_call()`
- `list_calls()`
- `get_call_recordings()`
- `download_recording()`
- `list_phone_numbers()`
- `list_applications()`
- `handle_webhook()`
- `process_callback()`
- `handle_event()`
- `batch_processor()`

Each method listed above gets its own subsection in Section 5 (Key API Endpoints & Methods) with:
- The exact API endpoint(s) it calls
- Request parameters and payload shape
- Response schema and pagination strategy
- How it maps to NormalizedDocument (for data-fetching methods)

## Selected Features — plan MUST include implementation strategy for each
- Retry Logic
- Rate Limiting
- Pagination
- Circuit Breaker
- Normaliser
- Structured Logging
- Webhook Router
- Signature Verification

## Config & Install Fields — document ALL of these in Section 8

🔒 NEVER hardcode credentials. `client_id`, `client_secret`, API keys, tokens, and passwords are ALWAYS user-provided install_fields read via `self.config.get("key")` — NEVER class constants, NEVER placeholder values like `CLIENT_ID = "your-...-id"`. For `oauth2_code` connectors, `client_id` (required) and `client_secret` (required, type "secret") MUST be install_fields even if not listed below.

### Hardcoded class constants (NOT user-supplied — same value for all tenants)
Hardcode these as class-level constants. Do NOT add to install_fields.
- `username` (API Username) → `USERNAME = ...` — research real value (hint: "api-user")
- `base_url` (Base URL) → `BASE_URL = ...` — research real value (hint: "https://messaging.bandwidth.com/api/v2")
- `rate_limit_per_min` (Rate Limit / min) → `RATE_LIMIT_PER_MIN = ...` — research real value (hint: "1800")
- `pagination_type` (Pagination Type) → `PAGINATION_TYPE = ...` — research real value (hint: "cursor")
- `api_version` (API Version) → `API_VERSION = ...` — research real value (hint: "v2")

### User-provided install fields (admin fills these at connector setup time)
These MUST appear as install_fields in metadata/connector.json. Read via `self.config.get("key")`.
- `account_id` (Account ID, required) → `self.config.get("account_id")`
- `username` (API Username, required) → `self.config.get("username")`
- `password` (API Password, required) → `self.config.get("password")`
- `webhook_secret` (Webhook Signing Secret, optional) → `self.config.get("webhook_secret")`

## Architecture Decisions
- Separation of concerns — connector.py orchestrates, HTTP is owned by client/http_client.py, normalisation by helpers/normalizer.py.
- Multi-tenant — every NormalizedDocument id is f"{self.tenant_id}_{source_id}"; carries tenant_id + connector_id.
- Three canonical base URLs: messaging.bandwidth.com/api/v2, voice.bandwidth.com/api/v2, dashboard.bandwidth.com/api — confirm camelCase payload conventions (applicationId, answerUrl, sourceTn).
- install() validates required config keys only — must NOT call health_check or any API; gateway calls health_check separately.

## Error Handling Patterns
- 401 → AuthStatus.TOKEN_EXPIRED + ConnectorHealth.OFFLINE
- 403 → AuthStatus.INVALID_CREDENTIALS + ConnectorHealth.UNHEALTHY
- 429 → ConnectorHealth.DEGRADED, log warning, honour Retry-After
- httpx.TimeoutException → ConnectorHealth.OFFLINE
- 5xx → retry with exponential backoff (max 3)


## Output file
Write the complete implementation plan to **`implementation_plan.md`** in the current working directory.

## Required sections in implementation_plan.md
1. **Overview** — what this connector does, which API it wraps, auth type, key capabilities
2. **SDK / Package Selection** — exact pip package names and versions to use; justify each choice
3. **Auth Flow** — step-by-step token acquisition, storage (Redis via `set_token()`), and refresh logic
4. **Data Model** — how API responses map to `NormalizedDocument` fields (field-by-field mapping)
5. **Key API Endpoints & Methods** — for EVERY method listed above: endpoint URL, HTTP method, request params, response schema, pagination strategy, and NormalizedDocument mapping
6. **Error Handling** — HTTP status codes to catch, exception types, fallback behaviour, retry strategy
7. **Dependencies** — exact `pip install` commands; these feed directly into the `install_deps` step
8. **Config & Install Fields** — every config key the connector reads from `self.config`, its type, required/optional, and where it comes from (install_field vs bind constant)
9. **SOC/OCP Architecture Plan** — file-by-file responsibility table (connector.py / client/http_client.py / helpers/normalizer.py / helpers/utils.py / exceptions.py) showing which logic lives where

## Rules
- Be specific: include exact SDK class names, method signatures, pagination token field names
- Section 5 MUST have a subsection for EVERY method listed in "MANDATORY — Methods to implement"
- Do NOT write any Python code yet — this is a planning document only
- Complete all 9 sections before finishing
- If a method name was explicitly requested by the user, it MUST appear by that exact name

Write implementation_plan.md now.
