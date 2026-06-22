# Discord Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Discord** is a chat / voice / community platform with a comprehensive REST API rooted at `https://discord.com/api/v10`. This connector — `DiscordConnector` (`CONNECTOR_TYPE = "discord"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Discord app/bot:

| Surface | Base path | Capability |
|---|---|---|
| Users | `/users/@me`, `/users/{user.id}` | Identify the bot/user, look up other users |
| Guilds | `/users/@me/guilds`, `/guilds/{guild.id}` | List + read guilds (servers) the bot belongs to |
| Channels | `/guilds/{guild.id}/channels`, `/channels/{channel.id}` | List + read text/voice/thread channels |
| Messages | `/channels/{channel.id}/messages` | Send / read / edit / delete channel messages |
| Members | `/guilds/{guild.id}/members` | List + read members, manage roles |
| Roles | `/guilds/{guild.id}/members/{user.id}/roles/{role.id}` | Add / remove role from member |
| Webhooks | `/channels/{channel.id}/webhooks`, `/webhooks/{webhook.id}/{webhook.token}` | Create + execute server-side webhooks |

The connector treats the **bot token as the API key** (the public `REQUIRED_CONFIG_KEYS = ["bot_token"]`, `AUTH_TYPE = "api_key"`), normalises messages into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff that honours Discord's `retry_after` field (3 attempts max), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::DiscordHTTPClient`).

> **Note on Bot vs OAuth tokens.** Discord supports two header forms:
>
> - `Authorization: Bot <bot_token>` — server-to-server bot apps. The default, controlled by `AUTH_TYPE = "api_key"`.
> - `Authorization: Bearer <oauth_token>` — when an OAuth2 user token is provided via `config["oauth_token"]`, the HTTP client switches to `Bearer`. Same client, same surface — only the header scheme changes. The bot path is the canonical install path; OAuth is an opt-in override for tenants that want to act on behalf of a user instead of a bot.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator for `DiscordRateLimitError` 429 handling |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Discord REST API supports two server-friendly auth schemes. The connector exposes a single install field (`bot_token`) and treats it as the api_key.

### Credentials
- `bot_token` — Discord bot token from **Developer Portal → Application → Bot → Reset Token**. install_field (type `secret`, required). Sent in the `Authorization` header with the `Bot ` prefix.
- `oauth_token` — Optional Bearer OAuth2 user-token override. install_field (type `secret`, optional). When set, the HTTP client uses `Bearer <oauth_token>` instead of `Bot <bot_token>`.
- `base_url` — Optional override of `https://discord.com/api/v10`. install_field (type `string`, optional).
- `rate_limit_per_min` — Soft client-side cap. install_field (type `number`, optional, default 50).

### Header contract
Every request to `https://discord.com/api/v10/*`:

```
Authorization: Bot <bot_token>            (default — bot apps)
            OR Bearer <oauth_token>       (when oauth_token is set)
User-Agent:    DiscordBot (https://shielva.ai, 1.0)
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `bot_token` is non-empty. Does **not** call the API.
- `authorize()` — no-op for api_key flow; returns an empty `TokenInfo` whose access_token is the configured bot_token.
- `health_check()` — `GET /users/@me` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle for bot tokens; OAuth refresh is out of scope for this revision).

## 4. Data Model

### 4.1 Message → NormalizedDocument

| NormalizedDocument | Discord JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{message['id']}"` | tenant-scoped |
| `source_id` | `message["id"]` | Discord snowflake |
| `title` | `f"Message in #{channel_id}"` | derived |
| `content` | `message["content"]` | plain text |
| `author` | `message["author"]["username"]` | |
| `created_at` | `message["timestamp"]` | RFC 3339 |
| `updated_at` | `message["edited_timestamp"]` | nullable |
| `metadata` | `{channel_id, guild_id, author_id, attachments, kind: "discord.message"}` | |

### 4.2 Guild → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{guild['id']}"` |
| `source_id` | `guild["id"]` |
| `title` | `guild["name"]` |
| `content` | description or empty |
| `metadata` | `{owner_id, member_count, kind: "discord.guild"}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/users/@me` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates guilds → channels → messages | Calls `ingest_document`. |
| `list_guilds(*, limit=100, before=None, after=None)` | GET | `/users/@me/guilds` | Snowflake cursor pagination. |
| `get_guild(guild_id)` | GET | `/guilds/{guild_id}` | |
| `list_channels(guild_id)` | GET | `/guilds/{guild_id}/channels` | |
| `get_channel(channel_id)` | GET | `/channels/{channel_id}` | |
| `send_message(channel_id, content, *, embeds=None, components=None)` | POST | `/channels/{channel_id}/messages` | |
| `get_message(channel_id, message_id)` | GET | `/channels/{channel_id}/messages/{message_id}` | |
| `list_messages(channel_id, *, limit=50, before=None, after=None, around=None)` | GET | `/channels/{channel_id}/messages` | |
| `edit_message(channel_id, message_id, content)` | PATCH | `/channels/{channel_id}/messages/{message_id}` | |
| `delete_message(channel_id, message_id)` | DELETE | `/channels/{channel_id}/messages/{message_id}` | |
| `list_guild_members(guild_id, *, limit=100, after=None)` | GET | `/guilds/{guild_id}/members` | |
| `get_user(user_id)` | GET | `/users/{user_id}` | |
| `add_role(guild_id, user_id, role_id)` | PUT | `/guilds/{guild_id}/members/{user_id}/roles/{role_id}` | |
| `remove_role(guild_id, user_id, role_id)` | DELETE | `/guilds/{guild_id}/members/{user_id}/roles/{role_id}` | |
| `create_webhook(channel_id, name)` | POST | `/channels/{channel_id}/webhooks` | |
| `execute_webhook(webhook_id, webhook_token, content, *, embeds=None)` | POST | `/webhooks/{webhook_id}/{webhook_token}` | |

Wire convention: Discord uses **snake_case** in JSON (`channel_id`, `guild_id`, `edited_timestamp`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Discord meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `DiscordBadRequestError` (raise) |
| 401 | Bot token revoked / OAuth token expired | `DiscordAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (missing intent/scope or 2FA required) | `DiscordAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `DiscordNotFoundError` (raise) |
| 429 | Rate limited — body `{retry_after: float, global: bool}` | `DiscordRateLimitError` → `ConnectorHealth.DEGRADED`, retry with the body's `retry_after` (capped) |
| 5xx | Provider outage | `DiscordServerError` → retry with exponential backoff |

All in `exceptions.py` extending `DiscordError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt` for 5xx, and the Discord-supplied `retry_after` for 429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `bot_token` | secret | yes | install_field | `Authorization: Bot <token>` header value |
| `oauth_token` | secret | no | install_field | When set, switches to `Authorization: Bearer <token>` |
| `base_url` | text | no | install_field (default `https://discord.com/api/v10`) | API base URL |
| `rate_limit_per_min` | number | no | install_field (default 50) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["bot_token"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers (Bot vs Bearer), retries (honours `retry_after`), raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Discord payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry` async helper, `safe_get`. | (stdlib only) |
| `models.py` | Pydantic schemas with snake_case aliases for request bodies. | `pydantic` |
| `exceptions.py` | `DiscordError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `DiscordConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, pagination, rate-limit honour) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
