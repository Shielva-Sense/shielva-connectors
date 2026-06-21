# Vonage Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

---

## 1. Overview

This connector integrates with **Vonage** (formerly Nexmo) — a CPaaS platform exposing a federated REST API suite across SMS, Voice, Verify (2FA), Numbers, Messages and Conversations. The connector — `VonageConnector` (`CONNECTOR_TYPE = "vonage"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs:

**Provider**: vonage
**Service**: vonage
**Auth Type**: `api_key` — but with **two distinct credential modes** (see Section 3):

1. **API key + secret** — for SMS, Account, Numbers, Verify, Search (HTTP Basic OR form-field credentials)
2. **JWT signed with RSA private key + application_id** — for Voice, Messages, Conversations

### API Surfaces Covered

| Surface | Base URL | Auth Mode | Capability |
|---|---|---|---|
| Account | `https://rest.nexmo.com` | api_key + secret | `get_balance` — credential probe |
| SMS | `https://rest.nexmo.com/sms/json` | api_key + secret (form) | `send_sms`, `get_sms_status`, `list_messages` |
| Numbers | `https://rest.nexmo.com/account/numbers`, `/number/*` | api_key + secret | `list_numbers`, `search_numbers`, `buy_number`, `cancel_number` |
| Verify | `https://api.nexmo.com/v2/verify` | api_key + secret | `send_verify_request`, `check_verify_code`, `cancel_verify` |
| Voice | `https://api.nexmo.com/v1/calls` | JWT + application_id | `create_call`, `get_call`, `list_calls`, `update_call`, `get_call_recording` |
| Applications | `https://api.nexmo.com/v2/applications` | api_key + secret | `list_applications` |
| Conversations | `https://api.nexmo.com/v0.3/conversations` | JWT + application_id | (forward-compatible surface) |

### Key Capabilities

- **SMS**: `send_sms`, `get_sms_status`, `list_messages`
- **Voice**: `create_call`, `get_call`, `list_calls`, `update_call`, `get_call_recording`
- **Verify**: `send_verify_request`, `check_verify_code`, `cancel_verify`
- **Numbers**: `list_numbers`, `search_numbers`, `buy_number`, `cancel_number`
- **Applications**: `list_applications`
- **Account**: `get_balance`
- **Webhooks**: `handle_webhook`, `process_callback` (JWT signature), `handle_event`, `batch_processor`
- **Features**: retry with exponential backoff, Retry-After honour on 429, structured logging, envelope-error parsing, **two-mode auth** (api_key/secret OR JWT signing)

---

## 2. SDK / Package Selection

### Core HTTP Client

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async-first HTTP client used by every CPaaS connector in this monorepo (matches Bandwidth/Wix). |

### Auth / Crypto

| Package | Version | Justification |
|---|---|---|
| `PyJWT[crypto]` | `>=2.8,<3.0` | RS256 JWT signing for Vonage Voice / Messages / Conversations. The `[crypto]` extra pulls in `cryptography` to handle the RSA private key. Bandwidth uses HTTP Basic only; Vonage Voice requires JWT. |

### Logging

| Package | Version | Justification |
|---|---|---|
| `structlog` | `>=24.0,<25.0` | Mandatory per CONNECTOR_SYSTEM_PROMPT — same sink as Bandwidth/Wix. |

### Already-Available (Base Framework)

The following are expected to be provided by the connector base framework and must **not** be re-declared in requirements.txt:
`pydantic`, `redis`, `asyncio`, base `BaseConnector` / `NormalizedDocument` / `AuthStatus` / `ConnectorHealth`.

---

## 3. Auth Flow — the API-key vs JWT split

Vonage is **federated**: different surfaces require different credentials. The connector accepts both at install time and routes each call to the right authenticator.

### Mode A — API key + secret (default)

Used for: SMS, Account, Numbers, Verify, Applications, Search.

- `api_key` + `api_secret` are sent as **HTTP Basic** header on every request:
  `Authorization: Basic <base64(api_key:api_secret)>`
- Legacy SMS / Verify endpoints **also** accept `api_key` and `api_secret` as form fields — we send both Basic header AND form fields so any internal routing path Vonage uses still authenticates.

### Mode B — JWT signed with RSA private key + application_id

Used for: Voice (`/v1/calls`), Messages, Conversations.

- The connector signs a short-lived JWT (60s TTL) per request:
  ```
  {
    "iat": <now>,
    "jti": <uuid4>,
    "exp": <now+60>,
    "application_id": <self.application_id>
  }
  ```
  signed with `RS256` using the user-supplied PEM private key.
- The signed JWT is sent as `Authorization: Bearer <jwt>`.
- This mode is **optional** at install — `application_id` + `private_key` are not in `REQUIRED_CONFIG_KEYS`. Voice methods raise a `VonageConfigError` early if the caller tries to use them without the JWT credentials.

### Lifecycle

| Phase | Behaviour |
|---|---|
| `install()` | Validates `api_key` + `api_secret`. If `application_id` + `private_key` present, JWT mode is wired. Does **not** call any Vonage endpoint. |
| `health_check()` | `GET /account/get-balance` — minimal probe of api_key+secret. |
| `sync()` | Aggregates recent SMS + Calls into NormalizedDocuments. SMS uses `/search/messages` (api_key mode); Calls use `/v1/calls` (JWT mode — skipped if JWT not configured). |
| `ensure_token()` | Not called — Vonage api_key has no expiry. JWT is re-minted per request. |
| `save_config()` | Stores api_key/api_secret/application_id; never private_key (kept in memory + sealed config). |

### 401 / 403 / 429 handling

| HTTP | Mapping |
|---|---|
| 401 | `VonageAuthError`, `ConnectorStatus(OFFLINE, TOKEN_EXPIRED)` |
| 403 | `VonageAuthError`, `ConnectorStatus(UNHEALTHY, INVALID_CREDENTIALS)` |
| 429 | `VonageRateLimitError` w/ `retry_after_s`, `ConnectorStatus(DEGRADED, CONNECTED)` |
| 5xx | `VonageServerError`, retry candidate |

### Envelope errors (HTTP 200 with non-zero `status`)

SMS `/sms/json` returns `{"messages":[{"status":"9", "error-text":"…"}]}` even with 200. The HTTP client parses these envelopes and raises:
- `status=="9"` → `VonageInsufficientFunds`
- `status in {"4","14"}` → `VonageAuthError`
- otherwise → `VonageError`

---

## 4. Data Model

All data-fetching methods that hit `sync()` produce `NormalizedDocument` instances.

### 4.1 Message → NormalizedDocument

| NormalizedDocument | Vonage JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{msg['message-id']}"` | tenant-scoped |
| `source_id` | `msg["message-id"]` | Vonage SMS ID |
| `title` | `f"SMS {msg['message-id']}"` | |
| `content` | `msg.get("body", "")` | SMS text |
| `source` | `"vonage.sms"` | |
| `created_at` | `_parse_dt(msg.get("date-received"))` | RFC 3339 |
| `metadata` | `{from, to, direction, price, network, status}` | |

### 4.2 Call → NormalizedDocument

| NormalizedDocument | Vonage JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{call['uuid']}"` | |
| `source_id` | `call["uuid"]` | |
| `title` | `f"Call {call['uuid']} ({direction}, {status})"` | |
| `content` | summary text | |
| `source` | `"vonage.voice"` | |
| `created_at` | `_parse_dt(call.get("start_time"))` | |
| `metadata` | `{direction, status, from, to, application_id, conversation_uuid, duration, price}` | |

---

## 5. Key API Endpoints & Methods

### Account
- `async get_balance() -> Dict` — `GET /account/get-balance`

### SMS
- `async send_sms(payload) -> Dict` — `POST /sms/json` with form body `{api_key, api_secret, from, to, text, type?}`. Envelope-checked.
- `async get_sms_status(message_id) -> Dict` — `GET /search/message?id=<id>` (api_key/secret in query).
- `async list_messages(*, date=None, to=None, ids=None) -> Dict` — `GET /search/messages` with cursor params.

### Voice (JWT mode)
- `async create_call(payload) -> Dict` — `POST /v1/calls`. Caller must supply `to` and (one of `ncco` | `answer_url`).
- `async get_call(call_uuid) -> Dict` — `GET /v1/calls/{uuid}`.
- `async list_calls(*, status=None, date_start=None, date_end=None, page_size=10, record_index=0) -> Dict` — `GET /v1/calls`.
- `async update_call(call_uuid, payload) -> Dict` — `PUT /v1/calls/{uuid}` (e.g. `{action: "hangup"}`).
- `async get_call_recording(recording_url) -> bytes` — `GET <recording_url>`; returns raw audio bytes.

### Verify (v2)
- `async send_verify_request(payload) -> Dict` — `POST /v2/verify`. Body specifies workflow (SMS/voice/email), brand, channel.
- `async check_verify_code(request_id, code) -> Dict` — `POST /v2/verify/{request_id}` with `{code}`.
- `async cancel_verify(request_id) -> Dict` — `DELETE /v2/verify/{request_id}`.

### Numbers
- `async list_numbers(*, country=None, pattern=None, search_pattern=0, features=None, size=10, index=1) -> Dict` — `GET /account/numbers`.
- `async search_numbers(country, *, pattern=None, type="mobile-lvn", features="SMS,VOICE", size=10) -> Dict` — `GET /number/search`.
- `async buy_number(country, msisdn) -> Dict` — `POST /number/buy`.
- `async cancel_number(country, msisdn) -> Dict` — `POST /number/cancel`.

### Applications
- `async list_applications(*, page_size=10, page=1) -> Dict` — `GET /v2/applications`.

### Lifecycle
- `async install() -> ConnectorStatus` — validates api_key + api_secret. JWT optional.
- `async health_check() -> ConnectorStatus` — `GET /account/get-balance`.
- `async sync(...) -> SyncResult` — aggregate SMS via /search/messages + Calls via /v1/calls.

### Webhooks
- `async handle_webhook(payload, headers) -> Dict` — route by event type.
- `async process_callback(payload, headers) -> Dict` — verify Vonage JWT signature when `webhook_secret` (HS256) is set.
- `async handle_event(event) -> Dict` — idempotent ack.
- `async batch_processor(items) -> Dict` — iterate events with error capture.

---

## 6. Pagination

| Endpoint | Pagination scheme |
|---|---|
| `/account/numbers` | `size` + `index` (1-based page index) |
| `/search/messages` | `page_size` + `page_index` (returned `next` link) |
| `/v1/calls` | `page_size` + `record_index` (0-based offset, returned `_links.next` URL) |
| `/v2/applications` | `page_size` + `page` |

`sync()` iterates the SMS and Call endpoints using these schemes, stopping when the response's items list is empty (or `_links.next` is absent).

---

## 7. Error Handling

Exception hierarchy in `exceptions.py`:

```
VonageError                      # base; carries status_code + response_body
├── VonageAuthError              # 401 / 403, envelope status 4 / 14
├── VonageBadRequestError        # 400
├── VonageNotFoundError          # 404
├── VonageConflictError          # 409
├── VonageRateLimitError         # 429 — retry_after_s
├── VonageServerError            # 5xx
├── VonageInsufficientFunds      # 402 / envelope status 9
└── VonageConfigError            # JWT mode required but private_key missing
```

Back-compat alias preserved:
```
VonageNetworkError = VonageServerError    # legacy name from older code
```

### Retry behaviour (`client/http_client.py::request`)

| Status | Action |
|---|---|
| 400 / 401 / 403 / 404 / 409 | Raise immediately |
| 429 | Honour `Retry-After` header; sleep then retry up to `max_retries=3` |
| 5xx | Exponential backoff `min(2 ** attempt, 8)`s, retry up to 3 |
| `httpx.TimeoutException` | Backoff, retry up to 3, then propagate |

---

## 8. Rate Limiting

Vonage's published quotas:
- SMS: 30 msgs/sec per account (Standard plan — varies)
- Voice: 100 concurrent calls (Standard)
- Verify: 1 req per number per 30s

The connector exposes `rate_limit_per_min` install-field as a **soft** client-side advisory only — Vonage enforces server-side. We honour `Retry-After` on 429 (see §7).

---

## 9. Webhook / Callback Signing

Vonage signs Voice + Messages event callbacks with **HS256-JWT** in the `Authorization: Bearer <jwt>` header when a webhook signing secret is configured in the Vonage Dashboard.

`process_callback`:
1. Reads `Authorization` header (fallback: `X-Vonage-Signature`).
2. Verifies the JWT against `self.config["webhook_secret"]` with HS256.
3. Returns `{verified: True/False, data: payload, error?: "…"}`.

If `webhook_secret` is empty, returns `{verified: True, unverified: True}` to allow dev-mode usage.

`handle_webhook` routes verified payloads to per-event handlers by `event_type`:
- `message:submitted` / `message:delivered` / `message:rejected`
- `call:started` / `call:answered` / `call:completed` / `recording:available`

Unknown events return `{status: "ignored", event_type: <…>}`.
