# Bandwidth CPaaS Connector — Implementation Plan

---

## 1. Overview

This connector integrates with Bandwidth's CPaaS platform to expose three distinct API surfaces under a single shared HTTP Basic credential pair. It enables messaging, voice call management, and phone number/application administration operations within a multi-tenant SaaS gateway.

**Provider**: bandwidth  
**Service**: bandwidth  
**Auth Type**: HTTP Basic (account_id in URL path + username:password header)

### API Surfaces Covered

| Surface | Base URL | Key Capabilities |
|---|---|---|
| Messaging | `https://messaging.bandwidth.com/api/v2` | Send/receive SMS/MMS, media management |
| Voice | `https://voice.bandwidth.com/api/v2` | Call lifecycle, recordings |
| Numbers / Dashboard | `https://dashboard.bandwidth.com/api` | Phone number inventory, application registry |

### Key Capabilities

- **Messaging**: `send_message`, `get_message`, `list_messages`, `list_media`, `upload_media`, `delete_media`
- **Voice**: `create_call`, `get_call`, `update_call`, `list_calls`, `get_call_recordings`, `download_recording`
- **Numbers**: `list_phone_numbers`, `list_applications`
- **Webhooks**: `handle_webhook`, `process_callback`, `handle_event`, `batch_processor`
- **Features**: retry with exponential backoff, rate limiting, cursor + page pagination, circuit breaker, structured logging, HMAC-SHA256 signature verification

---

## 2. SDK / Package Selection

### Core HTTP Client

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async-first HTTP client with connection pooling, timeout controls, and native support for streaming binary responses needed by `download_recording`. Replaces `requests` for async compatibility. |
| `httpx[http2]` | same constraint | HTTP/2 multiplexing reduces connection overhead when fanning out across three base URLs. |

### Retry / Resilience

| Package | Version | Justification |
|---|---|---|
| `tenacity` | `>=8.3,<9.0` | Composable retry decorator with `wait_exponential`, `stop_after_attempt`, and `retry_if_exception_type`. Keeps retry logic out of connector.py (OCP §9). |
| `circuitbreaker` | `>=2.0,<3.0` | Lightweight `@circuit` decorator for per-surface circuit breakers; raises `CircuitBreakerError` caught by exceptions.py. |

### Rate Limiting

| Package | Version | Justification |
|---|---|---|
| `aiolimiter` | `>=1.1,<2.0` | Token-bucket async rate limiter; wraps HTTP calls in `http_client.py` at 1 800 req/min ceiling. |

### Cryptography / Webhooks

| Package | Version | Justification |
|---|---|---|
| `cryptography` | `>=42.0,<43.0` | Provides `hmac` and `hashlib` wrappers used for timing-safe HMAC-SHA256 comparison in `process_callback`. (Standard library `hmac` is sufficient; include `cryptography` only if the base framework already requires it.) |

### Utilities

| Package | Version | Justification |
|---|---|---|
| `python-dateutil` | `>=2.9,<3.0` | ISO-8601 timestamp parsing for Bandwidth event timestamps. |
| `structlog` | `>=24.0,<25.0` | Structured JSON logging for observability; feeds the gateway's log sink. |

### Already-Available (Base Framework)

The following are expected to be provided by the connector base framework and must **not** be re-declared:
`pydantic`, `redis`, `asyncio`, base `ConnectorBase` / `NormalizedDocument` / `AuthStatus` / `ConnectorHealth`.

---

## 3. Auth Flow

### Credential Model

Bandwidth uses **HTTP Basic Authentication** on every request. There is no token exchange, no OAuth dance, and no expiry cycle — credentials are long-lived and user-managed.

The URL path always contains `{account_id}` as a path segment, e.g.:

```
GET /accounts/{account_id}/messages
Authorization: Basic base64(username:password)
```

### Step-by-Step

1. **`install()` validation** — On install, validate that `account_id`, `username`, and `password` are present in `self.config`. Raise `ConnectorConfigError` for any missing key. Do **not** call any Bandwidth API endpoint here.

2. **Credential resolution** — At request time, `http_client.py` calls:
   ```
   account_id = self.config.get("account_id")
   username   = self.config.get("username")
   password   = self.config.get("password")
   ```
   These are assembled into an `httpx.BasicAuth(username, password)` object injected into every request.

3. **Header construction** — `http_client.py` builds a shared `httpx.AsyncClient` instance per surface (messaging, voice, dashboard) with:
   - `auth=httpx.BasicAuth(username, password)`
   - `headers={"Accept": "application/json", "Content-Type": "application/json"}`
   - `timeout=httpx.Timeout(30.0)`

4. **No token storage** — Because HTTP Basic credentials don't expire, no `set_token()` / `get_token()` calls are needed. The raw credentials stay in `self.config` (provided by the gateway's secret store). If the framework mandates a `set_token` call, store a sentinel string `"basic"` with TTL=0 (never expires).

5. **401 / 403 handling** — On 401, `exceptions.py` raises `BandwidthAuthError(AuthStatus.TOKEN_EXPIRED)`. On 403, it raises `BandwidthAuthError(AuthStatus.INVALID_CREDENTIALS)`. `connector.py` catches `BandwidthAuthError` and sets `ConnectorHealth.OFFLINE` (401) or `ConnectorHealth.UNHEALTHY` (403) without retrying — credentials don't auto-refresh.

6. **`health_check()`** — Performs a lightweight authenticated GET against the Messaging surface (`/accounts/{account_id}/messages?limit=1`) to confirm credentials are valid and the service is reachable.

---

## 4. Data Model

All data-fetching methods return `NormalizedDocument` instances. Field mapping per surface:

### 4.1 Message → NormalizedDocument

| NormalizedDocument field | Bandwidth JSON field | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{message['id']}"` | Multi-tenant prefixed |
| `source_id` | `message["id"]` | Raw Bandwidth message ID |
| `tenant_id` | `self.tenant_id` | Gateway-injected |
| `connector_id` | `self.connector_id` | Gateway-injected |
| `type` | `"message"` | Fixed |
| `data.direction` | `message["direction"]` | `"in"` or `"out"` |
| `data.status` | `message["messageStatus"]` | camelCase source |
| `data.from` | `message["from"]` | E.164 source TN |
| `data.to` | `message["to"]` | List of E.164 destination TNs |
| `data.text` | `message["text"]` | Message body |
| `data.media` | `message.get("media", [])` | List of media URLs |
| `data.application_id` | `message["applicationId"]` | camelCase source |
| `data.created_time` | `message["time"]` | ISO-8601 |
| `data.segment_count` | `message.get("segmentCount", 1)` | |
| `raw` | `message` | Full raw payload |
| `created_at` | parsed `message["time"]` | datetime |
| `updated_at` | `datetime.utcnow()` | Ingestion time |

### 4.2 Call → NormalizedDocument

| NormalizedDocument field | Bandwidth JSON field | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{call['callId']}"` | |
| `source_id` | `call["callId"]` | |
| `type` | `"call"` | |
| `data.status` | `call["state"]` | e.g. `"active"`, `"completed"` |
| `data.from` | `call["from"]` | |
| `data.to` | `call["to"]` | |
| `data.direction` | `call["direction"]` | `"inbound"` or `"outbound"` |
| `data.answer_url` | `call["answerUrl"]` | camelCase |
| `data.application_id` | `call["applicationId"]` | |
| `data.start_time` | `call["startTime"]` | |
| `data.end_time` | `call.get("endTime")` | nullable |
| `data.duration` | `call.get("duration")` | seconds, nullable |
| `raw` | `call` | |

### 4.3 Call Recording → NormalizedDocument

| NormalizedDocument field | Bandwidth JSON field | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{rec['recordingId']}"` | |
| `source_id` | `rec["recordingId"]` | |
| `type` | `"recording"` | |
| `data.call_id` | `rec["callId"]` | |
| `data.duration` | `rec["duration"]` | |
| `data.file_format` | `rec["fileFormat"]` | e.g. `"wav"` |
| `data.media_url` | `rec["mediaUrl"]` | camelCase |
| `data.status` | `rec["status"]` | |
| `raw` | `rec` | |

### 4.4 Phone Number → NormalizedDocument

| NormalizedDocument field | Dashboard XML/JSON field | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{tn}"` | |
| `source_id` | `tn` (E.164 string) | |
| `type` | `"phone_number"` | |
| `data.number` | `TelephoneNumber` | Dashboard uses XML; normalizer parses |
| `data.status` | `Status` | |
| `data.city` | `City` | |
| `data.state` | `State` | |
| `data.rate_center` | `RateCenter` | |
| `raw` | parsed dict | |

### 4.5 Application → NormalizedDocument

| NormalizedDocument field | Dashboard JSON field | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{app['applicationId']}"` | |
| `source_id` | `app["applicationId"]` | |
| `type` | `"application"` | |
| `data.name` | `app["applicationDescription"]` | |
| `data.call_back_url` | `app["callbackUrl"]` | |
| `data.application_type` | `app["applicationType"]` | `"VOICE"` or `"MESSAGING_V2"` |
| `raw` | `app` | |

---

## 5. Key API Endpoints & Methods

### 5.1 `install()`

**Purpose**: Validate required config keys at connector setup time. Does NOT call any external API.

**Logic**:
- Assert `account_id`, `username`, `password` are present and non-empty in `self.config`.
- Raise `ConnectorConfigError("Missing required config key: {key}")` for any absence.
- Return `{"status": "installed"}` on success.

**Does not call any endpoint.**

---

### 5.2 `health_check()`

**Endpoint**: `GET https://messaging.bandwidth.com/api/v2/accounts/{account_id}/messages`  
**HTTP Method**: GET  
**Auth**: Basic  
**Params**: `limit=1`  
**Response**: JSON object with `messages` array (may be empty).  

**Logic**:
- Call `http_client.get("messaging", f"/accounts/{account_id}/messages", params={"limit": 1})`.
- On HTTP 200 → return `ConnectorHealth.HEALTHY`.
- On 401 → `ConnectorHealth.OFFLINE`.
- On 403 → `ConnectorHealth.UNHEALTHY`.
- On 429 → `ConnectorHealth.DEGRADED`.
- On timeout → `ConnectorHealth.OFFLINE`.

**NormalizedDocument mapping**: None (health probe only).

---

### 5.3 `sync()`

**Purpose**: Bulk-fetch recent data across all three surfaces and emit `NormalizedDocument` records to the gateway sink.

**Logic**:
1. Call `list_messages()` with a lookback window from last sync checkpoint.
2. Call `list_calls()` with same window.
3. Call `list_phone_numbers()`.
4. Call `list_applications()`.
5. Yield all resulting `NormalizedDocument` objects to the gateway via `self.emit(doc)`.

**Checkpointing**: Read `last_sync_at` from `self.config.get("_last_sync_at")` (gateway-managed). After completion, persist updated timestamp.

**Does not own pagination logic** — delegates entirely to the underlying list methods.

---

### 5.4 `send_message()`

**Endpoint**: `POST https://messaging.bandwidth.com/api/v2/users/{account_id}/messages`  
**HTTP Method**: POST  
**Auth**: Basic  

**Request Payload** (camelCase JSON):
```
{
  "to":            ["+15550001234"],
  "from":          "+15559998888",
  "text":          "Hello",
  "applicationId": "app-uuid",
  "media":         ["https://..."]   // optional
}
```

**Response** (HTTP 202):
```
{
  "id":            "1589228074636lm4k2je7j7jklbn2",
  "time":          "2020-04-07T20:21:14.636Z",
  "to":            ["+15550001234"],
  "from":          "+15559998888",
  "text":          "Hello",
  "applicationId": "app-uuid",
  "tag":           "custom-tag",
  "owner":         "+15559998888",
  "direction":     "out",
  "segmentCount":  1,
  "messageStatus": "pending"
}
```

**Pagination**: N/A (single response).  
**NormalizedDocument mapping**: See §4.1. Return the NormalizedDocument for the created message.

---

### 5.5 `get_message()`

**Endpoint**: `GET https://messaging.bandwidth.com/api/v2/users/{account_id}/messages/{message_id}`  
**HTTP Method**: GET  
**Auth**: Basic  

**Path Params**: `message_id` (string).  
**Response**: Single message object (same schema as §5.4 response).  
**Pagination**: N/A.  
**NormalizedDocument mapping**: See §4.1.

---

### 5.6 `list_messages()`

**Endpoint**: `GET https://messaging.bandwidth.com/api/v2/users/{account_id}/messages`  
**HTTP Method**: GET  
**Auth**: Basic  

**Query Params**:
| Param | Type | Description |
|---|---|---|
| `sourceTn` | string | Filter by source TN (camelCase) |
| `destinationTn` | string | Filter by destination TN |
| `messageStatus` | string | `pending`, `delivered`, `failed` |
| `messageDirection` | string | `INBOUND` or `OUTBOUND` |
| `carrierName` | string | Filter by carrier |
| `messageType` | string | `sms` or `mms` |
| `fromDateTime` | ISO-8601 string | Start of time window |
| `toDateTime` | ISO-8601 string | End of time window |
| `limit` | int | Max results per page (default 100, max 1000) |
| `pageToken` | string | Cursor token for next page |

**Pagination Strategy** — RFC 5988 `Link` header cursor:
- Parse `Link` response header for `rel="next"` URL.
- Extract `pageToken=` query param from that URL.
- Pass as `pageToken` on the next request.
- Stop when no `Link: rel="next"` header present.

**Response**:
```
{
  "totalCount": 1500,
  "pageInfo": {
    "prevPageToken": "...",
    "nextPageToken": "abc123"
  },
  "messages": [ { ...message objects... } ]
}
```

**NormalizedDocument mapping**: Each message → §4.1. Emit paginated batches.

---

### 5.7 `list_media()`

**Endpoint**: `GET https://messaging.bandwidth.com/api/v2/users/{account_id}/media`  
**HTTP Method**: GET  
**Auth**: Basic  

**Query Params**: `continuationToken` (cursor), `maxItems` (int, max 1000).  
**Pagination Strategy**: Check `Link` header for `rel="next"` cursor token.  

**Response** (JSON array):
```
[
  {
    "contentLength": 2763176,
    "mediaName":     "14155551212/0/9/8/0/7/",
    "content":       "https://messaging.bandwidth.com/api/v2/users/.../media/..."
  }
]
```

**NormalizedDocument mapping**:
| NormalizedDocument field | Source |
|---|---|
| `id` | `f"{tenant_id}_{item['mediaName']}"` |
| `type` | `"media"` |
| `data.media_name` | `item["mediaName"]` |
| `data.content_url` | `item["content"]` |
| `data.content_length` | `item["contentLength"]` |

---

### 5.8 `upload_media()`

**Endpoint**: `PUT https://messaging.bandwidth.com/api/v2/users/{account_id}/media/{media_id}`  
**HTTP Method**: PUT  
**Auth**: Basic  
**Content-Type**: Caller-supplied MIME type (e.g. `image/jpeg`, `audio/mp3`)  
**Body**: Raw binary bytes (not JSON)  

**Request Params**: `media_id` (path, URL-safe name), `content_type` (str), `data` (bytes).  
**Response**: HTTP 204 No Content on success.  
**NormalizedDocument mapping**: Return minimal doc with `type="media"`, `source_id=media_id`.

---

### 5.9 `delete_media()`

**Endpoint**: `DELETE https://messaging.bandwidth.com/api/v2/users/{account_id}/media/{media_id}`  
**HTTP Method**: DELETE  
**Auth**: Basic  
**Response**: HTTP 204 No Content on success.  
**NormalizedDocument mapping**: None. Return `{"deleted": True, "media_id": media_id}`.

---

### 5.10 `create_call()`

**Endpoint**: `POST https://voice.bandwidth.com/api/v2/accounts/{account_id}/calls`  
**HTTP Method**: POST  
**Auth**: Basic  

**Request Payload** (camelCase JSON):
```
{
  "to":            "+15550001234",
  "from":          "+15559998888",
  "answerUrl":     "https://my-app.com/webhooks/answer",
  "applicationId": "app-uuid",
  "answerMethod":  "POST",
  "callTimeout":   30,
  "tag":           "optional-tag"
}
```

**Response** (HTTP 201):
```
{
  "callId":        "c-15ac29a2-1331029c-2cb0-4a07-b215-b22865662d85",
  "accountId":     "9900000",
  "applicationId": "app-uuid",
  "to":            "+15550001234",
  "from":          "+15559998888",
  "state":         "initiated",
  "direction":     "outbound",
  "startTime":     "2020-01-01T00:00:00.000Z",
  "answerUrl":     "https://my-app.com/webhooks/answer"
}
```

**NormalizedDocument mapping**: See §4.2.

---

### 5.11 `get_call()`

**Endpoint**: `GET https://voice.bandwidth.com/api/v2/accounts/{account_id}/calls/{call_id}`  
**HTTP Method**: GET  
**Auth**: Basic  
**Path Params**: `call_id`.  
**Response**: Single call object (same schema as §5.10 response + `endTime`, `duration`).  
**NormalizedDocument mapping**: See §4.2.

---

### 5.12 `update_call()`

**Endpoint**: `POST https://voice.bandwidth.com/api/v2/accounts/{account_id}/calls/{call_id}`  
**HTTP Method**: POST  
**Auth**: Basic  

**Request Payload** (camelCase JSON — provide any combination):
```
{
  "state":      "completed",   // terminate the call
  "redirectUrl": "https://...",
  "tag":        "new-tag"
}
```

**Response**: HTTP 200 with updated call object.  
**NormalizedDocument mapping**: See §4.2.

---

### 5.13 `list_calls()`

**Endpoint**: `GET https://voice.bandwidth.com/api/v2/accounts/{account_id}/calls`  
**HTTP Method**: GET  
**Auth**: Basic  

**Query Params**:
| Param | Type | Description |
|---|---|---|
| `to` | string | Filter by destination number |
| `from` | string | Filter by source number |
| `minStartTime` | ISO-8601 | Start of time window |
| `maxStartTime` | ISO-8601 | End of time window |
| `state` | string | `active`, `completed`, etc. |
| `pageSize` | int | Max results per page |
| `pageToken` | string | Cursor for next page |

**Pagination Strategy** — RFC 5988 `Link` header cursor:
- Parse `Link` header for `rel="next"` URL.
- Extract `pageToken=` query param.
- Continue until no next link.

**Response**: JSON array of call objects.  
**NormalizedDocument mapping**: Each call → §4.2.

---

### 5.14 `get_call_recordings()`

**Endpoint**: `GET https://voice.bandwidth.com/api/v2/accounts/{account_id}/calls/{call_id}/recordings`  
**HTTP Method**: GET  
**Auth**: Basic  
**Path Params**: `call_id`.  

**Response** (JSON array):
```
[
  {
    "accountId":   "9900000",
    "callId":      "c-...",
    "recordingId": "r-...",
    "to":          "+15550001234",
    "from":        "+15559998888",
    "duration":    "PT6S",
    "direction":   "inbound",
    "channels":    1,
    "startTime":   "2020-01-01T00:00:00.000Z",
    "endTime":     "2020-01-01T00:00:06.000Z",
    "fileFormat":  "wav",
    "status":      "complete",
    "mediaUrl":    "https://voice.bandwidth.com/api/v2/accounts/.../calls/.../recordings/.../media"
  }
]
```

**Pagination**: No pagination on this endpoint (returns all recordings for a single call).  
**NormalizedDocument mapping**: See §4.3.

---

### 5.15 `download_recording()`

**Endpoint**: `GET https://voice.bandwidth.com/api/v2/accounts/{account_id}/calls/{call_id}/recordings/{recording_id}/media`  
**HTTP Method**: GET  
**Auth**: Basic  
**Path Params**: `call_id`, `recording_id`.  

**Response**: Raw binary audio bytes (`Content-Type: audio/wav` or `audio/mp3`).  
**Streaming**: Use `httpx` streaming response (`stream=True`) to handle large audio files without buffering entirely in memory.  
**Return**: `bytes` object (or async generator of chunks for streaming).  
**NormalizedDocument mapping**: Return raw bytes directly, not a NormalizedDocument.

---

### 5.16 `list_phone_numbers()`

**Endpoint**: `GET https://dashboard.bandwidth.com/api/accounts/{account_id}/tns`  
**HTTP Method**: GET  
**Auth**: Basic  
**Content-Type**: Dashboard API returns XML by default; set `Accept: application/json` or parse XML.  

**Query Params**:
| Param | Type | Description |
|---|---|---|
| `page` | int | Page number (1-based) |
| `size` | int | Page size (max 500) |
| `FullNumber` | string | Filter by specific TN |
| `City` | string | Filter by city |
| `State` | string | Filter by state code |

**Pagination Strategy** — page/size (not cursor):
- Start at `page=1`.
- Increment `page` until the response `TotalCount` ≤ `page * size`.
- Stop when result count < `size`.

**Response** (JSON or XML parsed):
```
{
  "TelephoneNumberResponse": {
    "TelephoneNumberCount": 3,
    "TelephoneNumbers": {
      "TelephoneNumber": ["+15550001234", "+15550001235", "+15550001236"]
    }
  }
}
```

**NormalizedDocument mapping**: See §4.4.

---

### 5.17 `list_applications()`

**Endpoint**: `GET https://dashboard.bandwidth.com/api/accounts/{account_id}/applications`  
**HTTP Method**: GET  
**Auth**: Basic  

**Query Params**: `page` (int), `size` (int).  
**Pagination Strategy**: page/size (same as §5.16).  

**Response**:
```
{
  "ApplicationList": {
    "Application": [
      {
        "ApplicationId":          "d775585a-ed5b-4a49-8b96-f68c0a993ebe",
        "ServiceType":            "Messaging-V2",
        "AppName":                "Production SMS",
        "MsgCallbackUrl":         "https://...",
        "CallbackCreds":          { "UserId": "...", "Password": "..." }
      }
    ]
  }
}
```

**NormalizedDocument mapping**: See §4.5.

---

### 5.18 `handle_webhook()`

**Purpose**: Entry-point for all inbound Bandwidth event callbacks (HTTP POST from Bandwidth servers).

**Input**: Raw HTTP request body (bytes or str) + headers dict.

**Logic**:
1. Extract `X-Callback-Signature` header.
2. Call `process_callback(body, signature)` — raises `BandwidthSignatureError` on mismatch.
3. Parse JSON body to dict.
4. Route on `event["type"]` (or `event["eventType"]`):
   - `"message-received"` → `_on_message_received(event)`
   - `"message-delivered"` → `_on_message_delivered(event)`
   - `"message-failed"` → `_on_message_failed(event)`
   - `"bridge-complete"` → `_on_bridge_complete(event)`
   - `"recording-available"` → `_on_recording_available(event)`
   - Unknown types → log warning, return `{"status": "unhandled", "type": event_type}`.
5. Return `{"status": "ok"}`.

---

### 5.19 `process_callback()`

**Purpose**: Verify `X-Callback-Signature` header via HMAC-SHA256 using `webhook_secret`.

**Inputs**: `body: bytes`, `signature: str`

**Logic**:
1. Read `webhook_secret = self.config.get("webhook_secret")`.
2. Compute `expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()`.
3. Use `hmac.compare_digest(expected, signature)` (timing-safe).
4. If mismatch → raise `BandwidthSignatureError("Invalid X-Callback-Signature")`.
5. If `webhook_secret` is not configured → log warning and skip verification (permissive mode).
6. Return `True` on success.

---

### 5.20 `handle_event()`

**Purpose**: Idempotency-keyed acknowledgement of a single processed Bandwidth event.

**Inputs**: `event_id: str`, `event: dict`, `handler_fn: Callable`

**Logic**:
1. Compute idempotency key: `f"bandwidth:event:{self.tenant_id}:{event_id}"`.
2. Check Redis: if key exists → return `{"status": "already_processed", "event_id": event_id}`.
3. Call `await handler_fn(event)`.
4. Set Redis key with TTL of 24 hours to mark event as processed.
5. Return `{"status": "processed", "event_id": event_id}`.

---

### 5.21 `batch_processor()`

**Purpose**: Process a list of Bandwidth events delivered in a single callback payload (batch webhook delivery).

**Inputs**: `events: list[dict]`

**Logic**:
1. For each event in `events`:
   a. Extract `event["messageId"]` or `event["eventId"]` as the idempotency key.
   b. Call `handle_event(event_id, event, handler)`.
   c. Collect result.
2. Return list of per-event results: `[{"event_id": ..., "status": ...}]`.
3. On partial failure: continue processing remaining events; collect errors; return aggregate report.

---

## 6. Error Handling

### HTTP Status Code Mapping

| Status | Exception | ConnectorHealth | Retry? |
|---|---|---|---|
| 400 | `BandwidthBadRequestError` | — | No |
| 401 | `BandwidthAuthError(AuthStatus.TOKEN_EXPIRED)` | `OFFLINE` | No |
| 403 | `BandwidthAuthError(AuthStatus.INVALID_CREDENTIALS)` | `UNHEALTHY` | No |
| 404 | `BandwidthNotFoundError` | — | No |
| 429 | `BandwidthRateLimitError` | `DEGRADED` | Yes — after `Retry-After` seconds |
| 500–503 | `BandwidthServerError` | `DEGRADED` | Yes — exponential backoff |
| Timeout | `BandwidthTimeoutError` | `OFFLINE` | Yes (1 retry) |

### Retry Strategy

- **429**: Read `Retry-After` header (integer seconds). `await asyncio.sleep(retry_after)` then retry once.
- **5xx**: Exponential backoff via `tenacity`:
  - `wait = wait_exponential(multiplier=1, min=2, max=30)`
  - `stop = stop_after_attempt(3)`
  - `retry = retry_if_exception_type(BandwidthServerError)`
- **4xx (except 429)**: Do not retry — deterministic failures.

### Circuit Breaker

- Per-surface `@circuit` decorator on `http_client._request()`.
- Threshold: 5 consecutive failures open the breaker.
- Half-open after 60 seconds.
- On open → raise `BandwidthCircuitOpenError` immediately (no HTTP call).
- `connector.py` catches `BandwidthCircuitOpenError` → set `ConnectorHealth.OFFLINE`, log, propagate.

### Error Delegation

All error mapping (status code → exception type → health enum) lives exclusively in `exceptions.py`. `connector.py` catches only `BandwidthError` (the base) and its subclasses — it never inspects raw HTTP status codes.

### Webhook Signature Failure

`BandwidthSignatureError` (subclass of `BandwidthError`) is raised by `process_callback()` on HMAC mismatch. `handle_webhook()` catches it, logs a security warning with the source IP, and returns HTTP 401 to the caller.

---

## 7. Dependencies

### Install Commands

```bash
pip install "httpx[http2]>=0.27,<1.0"
pip install "tenacity>=8.3,<9.0"
pip install "circuitbreaker>=2.0,<3.0"
pip install "aiolimiter>=1.1,<2.0"
pip install "python-dateutil>=2.9,<3.0"
pip install "structlog>=24.0,<25.0"
```

### Combined single-line install (for `install_deps` step)

```bash
pip install "httpx[http2]>=0.27,<1.0" "tenacity>=8.3,<9.0" "circuitbreaker>=2.0,<3.0" "aiolimiter>=1.1,<2.0" "python-dateutil>=2.9,<3.0" "structlog>=24.0,<25.0"
```

### `requirements.txt` block

```
httpx[http2]>=0.27,<1.0
tenacity>=8.3,<9.0
circuitbreaker>=2.0,<3.0
aiolimiter>=1.1,<2.0
python-dateutil>=2.9,<3.0
structlog>=24.0,<25.0
```

### Rationale Summary

| Package | Why needed |
|---|---|
| `httpx[http2]` | Async HTTP client with streaming support for binary downloads |
| `tenacity` | Composable retry decorator — keeps retry logic in `helpers/utils.py` |
| `circuitbreaker` | Per-surface circuit breaker — prevents cascade failures |
| `aiolimiter` | Token-bucket rate limiter at 1 800 req/min per Bandwidth limits |
| `python-dateutil` | ISO-8601 timestamp parsing for Bandwidth event times |
| `structlog` | Structured JSON logging for observability in multi-tenant deployments |

---

## 8. Config & Install Fields

### 8.1 Hardcoded Class Constants (NOT user-supplied)

These are identical for every tenant and every deployment. Define as class-level constants in `connector.py`:

```python
class BandwidthConnector(BaseConnector):
    MESSAGING_BASE_URL   = "https://messaging.bandwidth.com/api/v2"
    VOICE_BASE_URL       = "https://voice.bandwidth.com/api/v2"
    DASHBOARD_BASE_URL   = "https://dashboard.bandwidth.com/api"
    API_VERSION          = "v2"
    RATE_LIMIT_PER_MIN   = 1800
    PAGINATION_TYPE      = "cursor"   # Messaging + Voice surfaces
    MAX_RETRY_ATTEMPTS   = 3
    RETRY_BACKOFF_MIN    = 2          # seconds
    RETRY_BACKOFF_MAX    = 30         # seconds
    CIRCUIT_FAIL_THRESHOLD = 5
    CIRCUIT_RECOVERY_TIMEOUT = 60     # seconds
```

### 8.2 User-Provided Install Fields

These MUST appear in `metadata/connector.json` under `install_fields`. Each is read at runtime via `self.config.get("key")`:

| Key | Label | Type | Required | Description |
|---|---|---|---|---|
| `account_id` | Account ID | `string` | Yes | Numeric Bandwidth account identifier; appears in every URL path segment as `/accounts/{account_id}/...` |
| `username` | API Username | `string` | Yes | HTTP Basic auth username (typically the sub-account API username) |
| `password` | API Password | `secret` | Yes | HTTP Basic auth password; stored encrypted at rest by the gateway |
| `webhook_secret` | Webhook Signing Secret | `secret` | No | Used for HMAC-SHA256 verification of `X-Callback-Signature` headers; if absent, signature verification is skipped with a warning |

### 8.3 `metadata/connector.json` Shape (relevant excerpt)

```json
{
  "provider":  "bandwidth",
  "service":   "bandwidth",
  "auth_type": "basic",
  "install_fields": [
    {
      "key":      "account_id",
      "label":    "Account ID",
      "type":     "string",
      "required": true,
      "hint":     "Your numeric Bandwidth account ID (found in the Bandwidth Dashboard)"
    },
    {
      "key":      "username",
      "label":    "API Username",
      "type":     "string",
      "required": true,
      "hint":     "Your Bandwidth API username"
    },
    {
      "key":      "password",
      "label":    "API Password",
      "type":     "secret",
      "required": true,
      "hint":     "Your Bandwidth API password"
    },
    {
      "key":      "webhook_secret",
      "label":    "Webhook Signing Secret",
      "type":     "secret",
      "required": false,
      "hint":     "Shared secret for HMAC-SHA256 X-Callback-Signature verification (optional but recommended)"
    }
  ]
}
```

### 8.4 Gateway-Managed Config Keys (read-only, set by framework)

| Key | Source | Usage |
|---|---|---|
| `_last_sync_at` | Gateway checkpoint store | ISO-8601 timestamp of last successful `sync()` run; used as `fromDateTime` filter |
| `tenant_id` | `self.tenant_id` property | Multi-tenant document ID prefix |
| `connector_id` | `self.connector_id` property | Embedded in every `NormalizedDocument` |

---

## 9. SOC/OCP Architecture Plan

### File-by-File Responsibility Table

| File | Owns | Does NOT own |
|---|---|---|
| `connector.py` | Method orchestration; calling `http_client` + `normalizer` + `utils`; emitting `NormalizedDocument`; catching `BandwidthError` subclasses; setting `ConnectorHealth` | Raw HTTP calls; JSON parsing; retry logic; credential assembly |
| `client/http_client.py` | All `httpx.AsyncClient` lifecycle; `BasicAuth` header injection; URL construction (base URL + path); 429 `Retry-After` sleep; tenacity retry decoration; circuit breaker decoration; rate limiter acquisition; streaming binary responses | Business logic; data transformation; exception semantics above HTTP layer |
| `helpers/normalizer.py` | Mapping raw Bandwidth JSON/XML dicts to `NormalizedDocument` instances; multi-tenant ID prefixing (`f"{tenant_id}_{source_id}"`); ISO-8601 → `datetime` conversion; camelCase field extraction | HTTP calls; config access; logging |
| `helpers/utils.py` | RFC 5988 `Link` header parser (extract `rel="next"` cursor); page/size pagination iterator; `Retry-After` header extractor; ISO-8601 parser wrapper; idempotency key builder; batch chunker | HTTP calls; normalization; connector state |
| `exceptions.py` | All `BandwidthError` subclass definitions; HTTP status → exception mapping function `raise_for_status(response)`; `AuthStatus` + `ConnectorHealth` associations | HTTP calls; normalization; connector logic |

### SOC Compliance Verification (5/5)

| Check | Where it lives | Verified |
|---|---|---|
| `connector.py` has zero raw HTTP calls | All HTTP delegated to `client/http_client.py` | ✓ |
| All HTTP calls in `client/http_client.py` | Single `_request()` private method + per-verb helpers | ✓ |
| All transformations in `helpers/normalizer.py` | `normalize_message()`, `normalize_call()`, `normalize_recording()`, `normalize_phone_number()`, `normalize_application()` | ✓ |
| All utilities in `helpers/utils.py` | `parse_link_header()`, `page_iterator()`, `build_idempotency_key()` | ✓ |
| `connector.py` imports only from `client/` and `helpers/` | Never re-implements fetch, parse, or util logic | ✓ |

### OCP Compliance Verification (5/5)

| Check | Implementation | Verified |
|---|---|---|
| Each operation is a standalone `async def` | 21 individual methods; none folded into `sync()` | ✓ |
| New operations addable without modifying base | Subclass `BandwidthConnector`, add new method; no `sync()` or `BaseConnector` changes | ✓ |
| Config via `self.config.get("key")` | No hardcoded credentials or URLs in method bodies | ✓ |
| Features as composable helpers | `@retry_with_backoff` in `utils.py`, `@circuit` from `circuitbreaker`, `AsyncLimiter` in `http_client.py` | ✓ |
| Error mapping in `exceptions.py` | `raise_for_status()` owns all HTTP → exception mapping; `connector.py` catches named exceptions only | ✓ |

### Directory Layout

```
bandwidth_connector/
├── connector.py                  # Orchestration only
├── client/
│   ├── __init__.py
│   └── http_client.py            # httpx, BasicAuth, retry, circuit breaker, rate limit
├── helpers/
│   ├── __init__.py
│   ├── normalizer.py             # Raw dict → NormalizedDocument
│   └── utils.py                  # Link header parser, pagination, idempotency, batch
├── exceptions.py                 # BandwidthError hierarchy + raise_for_status()
└── metadata/
    └── connector.json            # install_fields, provider, auth_type
```

### Method → Module Call Graph

```
connector.py::send_message()
    └─► http_client.post("/users/{account_id}/messages", payload)
            └─► [rate limit] → [circuit breaker] → httpx.post()
                    └─► exceptions.raise_for_status(response)
    └─► normalizer.normalize_message(response_json, tenant_id)
    └─► return NormalizedDocument

connector.py::list_messages()
    └─► utils.page_iterator(http_client, "/users/{account_id}/messages", params)
            └─► [pagination loop via Link header]
                    └─► http_client.get(..., params={..., pageToken: cursor})
    └─► normalizer.normalize_message(msg, tenant_id) for each msg

connector.py::handle_webhook()
    └─► process_callback(body, signature)         # HMAC verify
            └─► utils.build_idempotency_key(...)
    └─► route on event["type"]
    └─► handle_event(event_id, event, handler_fn)
            └─► [Redis idempotency check]
```
