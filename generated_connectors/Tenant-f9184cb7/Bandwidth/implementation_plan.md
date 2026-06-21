# Bandwidth Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the canonical SAD pipeline (see `connector_development_docs/12-canonical-build-steps.md`).

## 1. Service Overview

[Bandwidth](https://www.bandwidth.com) is a CPaaS (Communications-Platform-as-a-Service) provider exposing three first-party surfaces:

| Surface | Purpose |
|---|---|
| **Messaging** | SMS / MMS send + receive, media storage |
| **Voice** | Programmable outbound calls, recording, BXML control |
| **Numbers / Dashboard** | Phone number inventory, applications, sub-accounts |

This connector covers the operational paths most ACP tenants need: send/receive messages, manage media attachments, place + manage outbound calls, fetch recordings, list numbers and applications.

## 2. Authentication

- **`auth_type`:** `basic_auth` (HTTP Basic — RFC 7617)
- **Credentials:**
  - `account_id` — Bandwidth account identifier (numeric, e.g. `5000123`); part of every URL path
  - `username` — API user (provisioned in Bandwidth Dashboard → API Credentials)
  - `password` — API password (NEVER hardcoded; stored as install_field type=`password`)
- **Header:** `Authorization: Basic base64(username:password)`
- **No OAuth, no token exchange** → `authorize()` is NOT implemented (per CONNECTOR_SYSTEM_PROMPT rule: `authorize()` only for `oauth2_code`/`oauth2_pkce`).
- **Optional MMS:** Some MMS flows additionally require an `application_id` per-message.

## 3. Base URLs (canonical — verified from Bandwidth API docs)

| Surface | Base URL | Path convention |
|---|---|---|
| Messaging | `https://messaging.bandwidth.com/api/v2` | `/users/{accountId}/{resource}` |
| Voice | `https://voice.bandwidth.com/api/v2` | `/accounts/{accountId}/{resource}` |
| Numbers / Dashboard | `https://dashboard.bandwidth.com/api` | `/accounts/{accountId}/{resource}` (some endpoints return XML; we set `Accept: application/xml` for those) |

All three surfaces share the same Basic-auth credential pair.

## 4. Endpoints catalogue (the methods this connector exposes)

| Public method | HTTP | Path | Surface |
|---|---|---|---|
| `install()` | (lifecycle) | n/a — calls `list_applications` for credential check | Dashboard |
| `health_check()` | GET | `/accounts/{accountId}/applications` (1-row limit) | Dashboard |
| `send_message()` | POST | `/users/{accountId}/messages` | Messaging |
| `get_message(message_id)` | GET | `/users/{accountId}/messages/{messageId}` | Messaging |
| `list_messages(filters)` | GET | `/users/{accountId}/messages` (paginated) | Messaging |
| `list_media()` | GET | `/users/{accountId}/media` | Messaging |
| `upload_media(media_id, content_type, data)` | PUT | `/users/{accountId}/media/{mediaId}` | Messaging |
| `delete_media(media_id)` | DELETE | `/users/{accountId}/media/{mediaId}` | Messaging |
| `create_call(payload)` | POST | `/accounts/{accountId}/calls` | Voice |
| `get_call(call_id)` | GET | `/accounts/{accountId}/calls/{callId}` | Voice |
| `update_call(call_id, payload)` | POST | `/accounts/{accountId}/calls/{callId}` | Voice |
| `list_calls(filters)` | GET | `/accounts/{accountId}/calls` (paginated) | Voice |
| `get_call_recordings(call_id)` | GET | `/accounts/{accountId}/calls/{callId}/recordings` | Voice |
| `download_recording(call_id, recording_id)` | GET | `/accounts/{accountId}/calls/{callId}/recordings/{recordingId}/media` | Voice |
| `list_phone_numbers(filters)` | GET | `/accounts/{accountId}/orders` (XML payload) | Dashboard |
| `list_applications()` | GET | `/accounts/{accountId}/applications` | Dashboard |
| `sync(since, full)` | (lifecycle) | iterates `list_messages` + `list_calls` → `NormalizedDocument` | composite |
| `handle_webhook(payload, headers)` | (lifecycle) | event router for inbound MMS / call events | composite |
| `process_callback(payload, headers)` | (lifecycle) | HMAC-SHA256 signature verification | composite |

**Payload convention:** Messaging + Voice use JSON with **camelCase** field names (`applicationId`, `answerUrl`, `sourceTn`, `mediaUrl`). Helpers map to/from snake_case for the connector boundary.

## 5. Pagination

| Surface | Mechanism | Token param |
|---|---|---|
| Messaging — `list_messages` | Cursor in `Link` header (`rel="next"`) + `pageToken` query param | `pageToken` |
| Voice — `list_calls` | Cursor in `Link` header + `pageToken` query param | `pageToken` |
| Dashboard — `list_applications`, `list_phone_numbers` | Page-number (`page`, `size`) | `page`, `size` |

`helpers/utils.py::parse_link_header()` extracts the `next` cursor; the connector loops until exhausted.

## 6. Rate limits

- Messaging API: 2000 messages/sec (account-wide), per-number throughput depends on tier.
- Voice API: 200 calls/sec (account-wide).
- Dashboard API: undocumented hard limit, treat as 100 req/sec safe.
- On `429 Too Many Requests`, Bandwidth returns `Retry-After` (seconds). The retry feature honours it via exponential backoff.

## 7. Dependencies (consumed by `install_deps`)

```
httpx>=0.27.0          # pre-installed
pydantic>=2.0          # pre-installed
structlog>=24.1        # pre-installed
tenacity>=8.2          # retry / backoff
```

`tenacity` is the only NEW package beyond the pre-installed common set.

## 8. Error model

| HTTP | Bandwidth meaning | Maps to |
|---|---|---|
| 400 | Bad request body | `BandwidthBadRequestError` → return `ConnectorStatus(health=DEGRADED, message=...)` |
| 401 | Bad Basic credentials | `BandwidthAuthError` → `AuthStatus.INVALID_CREDENTIALS` |
| 403 | Forbidden (account suspended) | `BandwidthAuthError` → `AuthStatus.FAILED` |
| 404 | Resource not found | `BandwidthNotFoundError` (re-raise) |
| 409 | Conflict (duplicate media id) | `BandwidthConflictError` |
| 429 | Rate limited | `BandwidthRateLimitError` → retry with `Retry-After` |
| 5xx | Provider outage | `BandwidthServerError` → retry up to N |

All exceptions inherit from `BandwidthError` (in `exceptions.py`).

## 9. Webhooks

Bandwidth posts events back to a configured callback URL with HMAC-SHA256 in `X-Callback-Signature`. Event types we route:

| Event type | Trigger |
|---|---|
| `message-received` | Inbound SMS/MMS |
| `message-delivered` | Outbound message delivered |
| `message-failed` | Outbound message failed |
| `bridge-complete` | Voice bridge ended |
| `recording-available` | Voice recording ready for download |

`process_callback()` verifies the signature with `hmac.compare_digest`. `handle_webhook()` routes by `eventType` to private `_handle_{event}()` stubs.

---

**Verified §7 dependencies before proceeding to `install_deps`.**
