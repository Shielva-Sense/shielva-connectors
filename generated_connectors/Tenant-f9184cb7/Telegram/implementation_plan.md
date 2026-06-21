# Telegram Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Telegram** exposes the **Bot API** under `https://api.telegram.org`. Unlike most REST providers there is no `Authorization` header — the bot token is part of the URL path itself (`/bot{bot_token}/{method}`) and the response is always wrapped in a uniform `{"ok": bool, "result": ..., "description": str?, "error_code": int?, "parameters": {...}}` envelope.

This connector — `TelegramConnector` (`CONNECTOR_TYPE = "telegram"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Telegram bot:

| Surface | Telegram method group | Capability |
|---|---|---|
| Updates | `getUpdates`, `setWebhook`, `deleteWebhook`, `getWebhookInfo` | Inbound delivery — long-polling **or** webhook |
| Messages | `sendMessage`, `sendPhoto`, `sendDocument`, `editMessageText`, `deleteMessage`, `forwardMessage` | Send + edit + delete messages and media |
| Chats | `getChat`, `getChatMember`, `getChatAdministrators` | Inspect chats and membership |
| Files | `getFile` | Resolve a file_id to a downloadable file path |
| Callback queries | `answerCallbackQuery` | Inline-keyboard button acks |
| Webhooks | provider-pushed | Telegram POSTs update JSON to the URL set by `setWebhook` |

The connector normalises inbound `Message` objects (from `getUpdates` or webhook) into `NormalizedDocument` (`id = f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), and routes webhook events through `handle_webhook → process_callback (X-Telegram-Bot-Api-Secret-Token verify) → handle_event → _handle_{event_kind}()`.

**Telegram-specific convention this connector documents and enforces:** the bot token is a **URL path segment**, not a header. The HTTP client builds `f"{base_url}/bot{bot_token}/{method}"` for every call; `connector.py` never touches the token in a header.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `pydantic` | `>=2.0` | Dataclass models for the Telegram surface; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `respx`, `pytest-mock`.

No third-party Telegram client (`python-telegram-bot`, `aiogram`, `telethon`) — they introduce conflicting event loops and obscure the Bot API surface we want exposed to tenants.

## 3. Auth Flow

Telegram Bot API uses **bot-token-in-URL** authentication. There is no header, no OAuth, no refresh.

### Credentials
- `bot_token` — issued by `@BotFather` (format `123456789:ABC...`). Stored as install_field (type `secret`, required).
- `webhook_url` — optional HTTPS URL Telegram will POST updates to. install_field (type `string`, optional). When set, `install()` calls `setWebhook` to register it.
- `webhook_secret_token` — optional secret echoed back in the `X-Telegram-Bot-Api-Secret-Token` header on every webhook POST so the receiver can authenticate the source. install_field (type `secret`, optional).

### URL contract
Every Bot API call:

```
{base_url}/bot{bot_token}/{api_method}
e.g. https://api.telegram.org/bot123456:ABC.../sendMessage
```

File downloads use a different prefix:
```
{base_url}/file/bot{bot_token}/{file_path}
```

### Lifecycle
- `install()` validates `bot_token` is non-empty, probes `getMe`, and (if `webhook_url` is set) registers the webhook via `setWebhook`.
- `authorize()` — NOT applicable for `api_key` flow; returns the bot_token wrapped in a `TokenInfo` for SDK ABI compatibility.
- `health_check()` — probes `getMe`.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Message → NormalizedDocument

| NormalizedDocument | Telegram JSON | Notes |
|---|---|---|
| `id` | `f"{connector_id}_{chat_id}_{message_id}"` | tenant-isolated via connector_id (which is itself tenant-scoped upstream) |
| `source_id` | `str(message["message_id"])` | Telegram message id |
| `title` | `f"Telegram message in {chat_title}"` | derived from `chat.title / username / first_name / id` |
| `content` | `message["text"]` or `message["caption"]` | text payload |
| `source` | `"telegram"` | fixed |
| `author` | `@username` or `"First Last"` from `message.from` | |
| `created_at` | `datetime.fromtimestamp(message["date"], tz=UTC)` | Unix epoch |
| `metadata` | `{chat_id, chat_type, chat_title, from_user_id, from_username, entities, reply_to_message_id}` | |

### 4.2 Webhook Update → handled by `handle_webhook`

Telegram POSTs a JSON body shaped like:
```json
{
  "update_id": 100,
  "message": { ... }            // or edited_message / channel_post / callback_query / inline_query
}
```

`handle_webhook(payload, headers)`:
1. Calls `process_callback(payload, headers)` to verify the `X-Telegram-Bot-Api-Secret-Token` header matches the install_field `webhook_secret_token` (constant-time compare via `hmac.compare_digest`).
2. Picks the populated update subfield (`message`, `edited_message`, `channel_post`, `edited_channel_post`, `callback_query`).
3. Forwards to `handle_event({"id": update_id, "type": <kind>, "data": <payload>})` for idempotent dispatch.
4. Returns `{"status": "processed", "kind": ..., "update_id": ...}` or `{"status": "ignored", ...}`.

## 5. Key API Endpoints & Methods

Every method below is a standalone public `async def` on `TelegramConnector`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a + `getMe` + optional `setWebhook` | Validate config; probe; optionally register webhook. |
| `health_check()` | GET | `/getMe` | Probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates `getUpdates` | Calls `ingest_document` per message; advances `last_update_id` checkpoint. |
| `get_me()` | GET | `/getMe` | Bot identity. |
| `send_message(chat_id, text, parse_mode?, ...)` | POST | `/sendMessage` | |
| `send_photo(chat_id, photo_url, caption?, parse_mode?)` | POST | `/sendPhoto` | |
| `send_document(chat_id, document_url, caption?, parse_mode?)` | POST | `/sendDocument` | |
| `edit_message(chat_id, message_id, text, parse_mode?)` | POST | `/editMessageText` | |
| `delete_message(chat_id, message_id)` | POST | `/deleteMessage` | bool |
| `forward_message(chat_id, from_chat_id, message_id)` | POST | `/forwardMessage` | |
| `get_updates(offset?, limit?, timeout?)` | GET | `/getUpdates` | Long polling when `timeout>0`. |
| `set_webhook(url, secret_token?, allowed_updates?)` | POST | `/setWebhook` | bool |
| `delete_webhook(drop_pending_updates?)` | POST | `/deleteWebhook` | bool |
| `get_webhook_info()` | GET | `/getWebhookInfo` | |
| `get_chat(chat_id)` | GET | `/getChat` | |
| `get_chat_member(chat_id, user_id)` | GET | `/getChatMember` | |
| `get_chat_administrators(chat_id)` | GET | `/getChatAdministrators` | |
| `get_file(file_id)` | GET | `/getFile` | Returns `{file_id, file_path, ...}`. Use `http_client.file_url(...)` to build download URL. |
| `answer_callback_query(callback_query_id, text?, show_alert?)` | POST | `/answerCallbackQuery` | |
| `handle_webhook(payload, headers)` | (lifecycle) | route by populated subfield | Calls `process_callback` first. |
| `process_callback(payload, headers)` | (lifecycle) | constant-time secret-token compare | Reads install_field `webhook_secret_token`. |
| `handle_event(event)` | (lifecycle) | idempotency-keyed ack | Dispatches to `_handle_message`, `_handle_callback_query`, etc. |
| `batch_processor(items)` | (lifecycle) | per-item event processing | |

Wire convention: Telegram uses **snake_case** in JSON (`message_id`, `chat_id`, `from_user`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads. Envelope unwrap (`ok / result`) is owned by `client/http_client.py`.

## 6. Error Handling

| HTTP / envelope.error_code | Telegram meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `TelegramBadRequestError` (raise) |
| 401 | Bot token invalid / revoked | `TelegramAuthError` → `AuthStatus.EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (bot kicked / blocked) | `TelegramForbiddenError` |
| 404 | Method not found / wrong endpoint | `TelegramNotFound` (raise) |
| 409 | Conflict (concurrent `getUpdates` vs webhook) | `TelegramConflictError` |
| 429 | Rate limited — `parameters.retry_after` is **always** present | `TelegramRateLimitError(retry_after=...)` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `TelegramServerError` → retry with exponential backoff |
| network | DNS / timeout / connection reset | `TelegramNetworkError` |

All in `exceptions.py` extending `TelegramError`. Retry in `helpers/utils.py::with_retry` honours:
- `retry_after` hint from 429 envelopes (Telegram-specific — always supplied)
- exponential backoff `base_delay * 2 ** attempt + jitter` for network / 5xx, capped at `MAX_RETRY_DELAY_S`

## 7. Dependencies

Connector-specific packages to install in connector's venv (`install_deps` reads this section):

```
# all required runtime deps (httpx, structlog, pydantic) are pre-installed in the shared venv
```

(httpx, structlog, pydantic, pytest, pytest-asyncio, respx, pytest-mock are pre-installed in the shared venv. Telegram has no extra runtime deps.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `bot_token` | secret | yes | install_field | URL path segment; from `@BotFather` |
| `webhook_url` | text | no | install_field | If set, `install()` calls `setWebhook` |
| `webhook_secret_token` | secret | no | install_field | Verified in `process_callback` via constant-time compare against `X-Telegram-Bot-Api-Secret-Token` header |
| `base_url` | text | no | install_field (default `https://api.telegram.org`) | Sandbox / self-hosted Bot API server override |
| `default_parse_mode` | text | no | install_field (default `HTML`) | Default for `sendMessage` / `sendPhoto` / etc. when caller doesn't override |
| `rate_limit_per_min` | number | no | install_field (default 1800) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["bot_token"]
_STATUS_MAP = {
    401: ("OFFLINE",   "EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle + webhook routing. **No raw HTTP, no JSON parsing, no envelope unwrap.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds `f"{base}/bot{token}/{method}"`, unwraps `{ok, result}` envelope, maps error codes to typed exceptions. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Telegram payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry` honouring `retry_after`; tiny `safe_get` walker. | `httpx`, `structlog`, `exceptions` |
| `models.py` | Pydantic / dataclass schemas for Telegram User, Chat, Message, WebhookInfo. | `pydantic` |
| `exceptions.py` | `TelegramError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `TelegramConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Envelope unwrap centralised in `client/http_client.py::_request` ✓
4. Response transforms in `helpers/normalizer.py` ✓
5. Utilities in `helpers/utils.py` ✓
6. `connector.py` imports from `client/` + `helpers/` ✓
7. Every user-named method is a standalone `async def` ✓
8. New ops added without modifying BaseConnector ✓
9. Config via `self.config.get(...)` ✓
10. Features (retry, normalizer, webhook routing) as composable helpers ✓
11. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓
12. Multi-tenant: `NormalizedDocument.id` = `f"{connector_id}_{chat_id}_{message_id}"`; connector_id is tenant-scoped upstream ✓

**Score: 10/10.**
