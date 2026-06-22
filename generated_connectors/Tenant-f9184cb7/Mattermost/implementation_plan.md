# Mattermost Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Mattermost** is an open-source, self-hostable team messaging platform (Slack-equivalent) exposing a REST API under `{server_url}/api/v4`. Unlike SaaS APIs, the base URL is **tenant-specific** — each customer provides their own server hostname. This connector — `MattermostConnector` (`CONNECTOR_TYPE = "mattermost"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Mattermost deployment:

| Surface | Base path | Capability |
|---|---|---|
| System | `/system/ping` | Liveness probe (no auth required) |
| Users | `/users` | Get me, list/get/create users, search |
| Teams | `/teams` | List + read teams the token has access to |
| Channels | `/channels`, `/teams/{id}/channels` | List + read + create + delete channels, manage members |
| Posts | `/posts` | Create / read / update / delete posts; threaded replies; channel timeline |
| Files | `/files` | Upload + download + metadata for attachments |
| Webhooks | `/hooks/incoming`, `/hooks/outgoing` | Create / list incoming + outgoing webhooks |
| Bots | `/bots` | Provision bot accounts and list active bots |
| Commands | `/commands` | List slash-commands installed on a team |
| Roles | `/roles` | Read role definitions for permissions checks |

The connector normalises posts + channels into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff that honours `Retry-After` / `X-RateLimit-Reset` headers, and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::MattermostHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas for typed envelopes; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

The Mattermost API uses simple Bearer tokens — no OAuth refresh, no JWT verification, so no `PyJWT` / `tenacity` is needed.

## 3. Auth Flow

Mattermost REST API uses **server-to-server Personal Access Token (PAT) authentication** for the bot/integration use case. A PAT is a long-lived bearer token minted from **Account Settings → Security → Personal Access Tokens** (requires system-admin opt-in on the server side).

### Credentials
- `server_url` — Tenant-specific base URL (e.g. `https://mattermost.acme.com`). install_field (type `string`, required). The connector strips any trailing `/api/v4` suffix and trailing slashes via `helpers/utils.py::normalize_server_url`.
- `personal_access_token` — Long-lived PAT or bot token. install_field (type `secret`, required).
- `default_team_id` — 26-char team ID to default to for actions that need one. install_field (type `string`, optional).
- `rate_limit_per_min` — Soft client-side cap. install_field (type `number`, default 200).

### Header contract
Every request to `{server_url}/api/v4/*`:

```
Authorization: Bearer <personal_access_token>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `server_url` + `personal_access_token` are non-empty, then probes `GET /users/me` to confirm the token before persisting config + token.
- `authorize()` — No OAuth code exchange. Re-probes `/users/me` and returns a `TokenInfo` carrying the stored PAT (parity with OAuth connectors).
- `health_check()` — `GET /system/ping` as a lightweight probe (no auth required by spec, but the connector still sends the auth header).
- `ensure_token()` — N/A (PATs do not expire unless revoked).

## 4. Data Model

### 4.1 Post → NormalizedDocument

| NormalizedDocument | Mattermost JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{post['id']}"` | tenant-scoped |
| `source_id` | `post["id"]` | 26-char post ID |
| `title` | `f"Post in {channel_id}"` | derived |
| `content` | `post["message"]` | markdown body |
| `content_type` | `"text/markdown"` | |
| `author` | `post["user_id"]` | |
| `created_at` | `datetime.fromtimestamp(post["create_at"]/1000, tz=UTC)` | ms epoch |
| `updated_at` | `datetime.fromtimestamp(post["update_at"]/1000, tz=UTC)` | ms epoch |
| `metadata` | `{channel_id, root_id, type, hashtags, file_ids, props, kind: "mattermost.post"}` | |

### 4.2 Channel → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{channel['id']}"` |
| `source_id` | `channel["id"]` |
| `title` | `channel["display_name"] or channel["name"]` |
| `content` | `channel.get("purpose", "") + "\n" + channel.get("header", "")` |
| `created_at` | `datetime.fromtimestamp(channel["create_at"]/1000, tz=UTC)` |
| `metadata` | `{name, team_id, type, member_count, total_msg_count, kind: "mattermost.channel"}` |

### 4.3 User → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{user['id']}"` |
| `source_id` | `user["id"]` |
| `title` | `user["username"]` |
| `content` | concat first_name + last_name + email + nickname |
| `author` | `user["email"]` |
| `metadata` | `{roles, locale, position, kind: "mattermost.user"}` |

## 5. Key API Endpoints & Methods

Every method listed in `metadata/connector.json::apis` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config + probe `/users/me`. |
| `health_check()` | GET | `/system/ping` | Liveness probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates teams + channels + recent posts | Calls `ingest_document`. |
| `get_me()` | GET | `/users/me` | Authenticated user. |
| `list_users(page, per_page, in_team_id, in_channel_id)` | GET | `/users` | Paginated. |
| `get_user(user_id)` | GET | `/users/{user_id}` | |
| `create_user(payload)` | POST | `/users` | Admin-only. |
| `search_users(team_id, term, ...)` | POST | `/users/search` | |
| `list_teams(page, per_page, include_total_count)` | GET | `/teams` | |
| `get_team(team_id)` | GET | `/teams/{team_id}` | |
| `list_channels(team_id, page, per_page, include_deleted)` | GET | `/teams/{team_id}/channels` | |
| `get_channel(channel_id)` | GET | `/channels/{channel_id}` | |
| `create_channel(team_id, name, display_name, type, purpose, header)` | POST | `/channels` | type `O` or `P`. |
| `delete_channel(channel_id)` | DELETE | `/channels/{channel_id}` | Soft-delete. |
| `add_user_to_channel(channel_id, user_id)` | POST | `/channels/{channel_id}/members` | |
| `list_channel_posts(channel_id, page, per_page, since, before, after)` | GET | `/channels/{channel_id}/posts` | |
| `post_message(channel_id, message, root_id?, props?, file_ids?)` | POST | `/posts` | Threaded via `root_id`. |
| `get_post(post_id)` | GET | `/posts/{post_id}` | |
| `update_post(post_id, message?, props?)` | PUT | `/posts/{post_id}` | |
| `delete_post(post_id)` | DELETE | `/posts/{post_id}` | |
| `upload_file(channel_id, file_bytes, filename)` | POST | `/files` | multipart/form-data. |
| `get_file_info(file_id)` | GET | `/files/{file_id}/info` | |
| `create_incoming_webhook(channel_id, display_name, description, username?, icon_url?)` | POST | `/hooks/incoming` | |
| `list_incoming_webhooks(team_id, page, per_page)` | GET | `/hooks/incoming` | |
| `create_outgoing_webhook(team_id, display_name, trigger_words, callback_urls, channel_id?)` | POST | `/hooks/outgoing` | |
| `list_outgoing_webhooks(team_id, page, per_page)` | GET | `/hooks/outgoing` | |
| `list_bots(page, per_page, include_deleted)` | GET | `/bots` | |
| `list_team_commands(team_id, custom_only)` | GET | `/commands` | |

Wire convention: Mattermost uses **snake_case** in JSON (`channel_id`, `user_id`, `display_name`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Mattermost meaning | Mapped to |
|---|---|---|
| 400 | Bad request (validation) | `MattermostBadRequestError` (raise) |
| 401 | Token invalid / missing | `MattermostAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` (health_check) / `INVALID_CREDENTIALS` + `OFFLINE` (install) |
| 403 | Permission denied | `MattermostAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found (bad route or missing resource) | `MattermostNotFound` (raise) |
| 409 | Conflict (duplicate channel name) | `MattermostConflictError` |
| 429 | Rate limited; `Retry-After` + `X-RateLimit-Reset` headers | `MattermostRateLimitError` → exponential backoff retry, honours `Retry-After` |
| 5xx | Provider outage | `MattermostNetworkError` → retry with exponential backoff |
| transport (timeout, DNS, conn refused) | n/a | `MattermostNetworkError` → retry |

All in `exceptions.py` extending `MattermostError`. Retry in `client/http_client.py::_request` honours `max_retries=3` (default), exponential backoff `min(0.5 * 2^attempt + jitter, 16s)`, with override from `Retry-After` when present.

`_STATUS_MAP` (class constant on `MattermostConnector`):

```python
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads `requirements.txt`):

```
httpx>=0.27,<1.0
pydantic>=2.0
structlog>=24.1
```

(`pytest`, `pytest-asyncio`, `pytest-mock`, `respx` are pre-installed via the shared dev venv.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `server_url` | string | yes | install_field | Tenant-specific base URL — `https://mm.example.com` |
| `personal_access_token` | secret | yes | install_field | `Authorization: Bearer <PAT>` |
| `default_team_id` | string | no | install_field | Default team for shorthand calls |
| `rate_limit_per_min` | number | no | install_field (default 200) | Soft cap; server-side 429s still honoured |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["server_url", "personal_access_token"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers, retries on 429/5xx, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Mattermost payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `normalize_server_url`, `safe_int`, `extract_message`, `ms_to_dt`. | (stdlib only) |
| `models.py` | Pydantic schemas for request envelopes (CreateChannel, PostMessage, ...). | `pydantic` |
| `exceptions.py` | `MattermostError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `MattermostConnector`. | `connector` |

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
