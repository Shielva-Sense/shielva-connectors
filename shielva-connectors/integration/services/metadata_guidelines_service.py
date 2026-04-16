"""Integration Builder — METADATA_WRITING_GUIDELINES service.

Manages metadata_writing_guideline.md: the standard that defines exactly how
the LLM must generate `metadata/connector.json` for every connector.

Storage hierarchy: Redis cache → R2 bucket → embedded default.
Every save creates a new MongoDB version record.
Seeded into the global codegen-guidelines-global KB on startup so Gemini
can retrieve it during the generate_metadata step via RAG.
"""

import asyncio
from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, List

import structlog

from integration.core.config import settings

logger = structlog.get_logger(__name__)

# ── R2 / Redis keys ──────────────────────────────────────────────────
_R2_PREFIX       = "METADATA_WRITING_GUIDELINES"
_STANDARD_KEY    = "metadata_writing_guideline.md"
_VERSIONED_KEY   = "metadata_writing_guideline_v_{version}.md"
_REDIS_KEY_TPL   = "metadata_writing_guidelines:v{version}"

# Sentinel — if missing from the active record, auto-upgrade on next boot
_SENTINEL = "name: ALWAYS Shielva {service_name} Connector"

# ── Default guideline content ────────────────────────────────────────

DEFAULT_METADATA_WRITING_MD = """\
# Connector Metadata Writing Guideline

> Managed by Shielva Integration Builder — auto-seeded into RAG on startup.
> Every `generate_metadata` step reads this via the knowledge base before calling the LLM.

---

## Purpose

This document tells the LLM exactly how to produce `metadata/connector.json`.
Follow every rule precisely — violations cause the deploy form to show wrong fields,
missing credentials, or broken OAuth flows.

---

## 1. Full Schema

```json
{
  "connector_type": "<exact CONNECTOR_TYPE class attribute value — e.g. shielva_gmail_connector>",
  "name":           "<Full product name — ALWAYS 'Shielva {ServiceName} Connector', e.g. 'Shielva Gmail Connector'>",
  "display_name":   "<Short service name only — e.g. 'Gmail', 'Slack', 'Salesforce'>",
  "version":        "<semver string, e.g. '1.0.0'>",
  "description":    "<one sentence: what this connector connects to and what it does>",
  "auth_type":      "<api_key | oauth2 | bearer_token | service_account>",
  "install_fields": [ ... ],
  "apis":           [ ... ],
  "painter": {
    "painter_type": "form",
    "config": {
      "title":        "Connect to <display_name>",
      "submit_label": "Connect",
      "fields":       "<copy of install_fields array>"
    }
  }
}
```

### CRITICAL: `name` vs `display_name`

| Field | Value | Example |
|-------|-------|---------|
| `name` | Full product name shown in the UI listing — ALWAYS "Shielva {ServiceName} Connector" | `"Shielva Gmail Connector"` |
| `display_name` | Short service name only | `"Gmail"` |

**`name` MUST be present** — this is what shows in the connector listing page.
If `name` is missing, the UI will fall back to the unformatted `connector_name` from the session (e.g. "gmail").

---

## 2. install_fields — Critical Rules

`install_fields` is the array of form fields shown to the user during Deploy step.

### 2.0 Field object structure

Every entry in `install_fields` (and `painter.config.fields`) must have these properties:

| Property | Description | Example |
|----------|-------------|---------|
| `key` | The config dict key — matches `self.config.get("key_name")` in connector.py | `"client_id"` |
| `label` | The human-readable UI label shown above the input | `"Google Client ID"` |
| `type` | Input type — see section 2.5 | `"text"` |
| `required` | true/false | `true` |
| `placeholder` | Realistic example value (shown greyed in empty input) | `"123456..."` |
| `help` | One sentence shown below the field | `"OAuth 2.0 Client ID..."` |
| `suggestions` | (optional) Clickable preset chips — see section 2.6 | `[{...}]` |
| `multi_select` | (optional, boolean) true = clicking a chip appends, false = replaces | `true` |

`key` and `label` are always BOTH present and DIFFERENT:
- `key` = the Python config dict key (lowercase_snake_case)
- `label` = the UI label (Title Case, human-readable)

### 2.1 What MUST be included

Every key that is **read from `self.config` or the `config` argument** anywhere in the
connector class (install, authorize, health_check, sync, or any helper) MUST appear as a
field — including keys read in `__init__` from the passed `config` dict.

### 2.2 What MUST NOT be included

**NEVER include `redirect_uri` in install_fields.**
The gateway injects `redirect_uri` automatically into `connector.config` at runtime before
calling `authorize()`. It is never entered by the user in the deploy form.

### 2.3 OAuth2 connectors — MANDATORY fields

**OAuth2 connectors MUST include client_id and client_secret in install_fields.**
These are required for the OAuth flow and must be entered by the user at deploy time.

```json
{
  "key":         "client_id",
  "label":       "Google Client ID",
  "type":        "text",
  "required":    true,
  "placeholder": "123456789-abc.apps.googleusercontent.com",
  "help":        "OAuth 2.0 Client ID from Google Cloud Console."
},
{
  "key":         "client_secret",
  "label":       "Google Client Secret",
  "type":        "password",
  "required":    true,
  "placeholder": "GOCSPX-...",
  "help":        "OAuth 2.0 Client Secret from Google Cloud Console."
}
```

> These fields must come FIRST in `install_fields`, before optional fields like scopes or sync_query.

### 2.4 NEVER read OAuth credentials from os.environ

The connector's `__init__` must read `client_id`/`client_secret` from the passed `config`
dict (with optional env-var fallback), NOT exclusively from `os.environ`:

```python
# CORRECT
cfg = config or {}
self.client_id     = cfg.get("client_id")     or os.environ.get("SERVICE_CLIENT_ID")
self.client_secret = cfg.get("client_secret") or os.environ.get("SERVICE_CLIENT_SECRET")

# WRONG — never do this
self.client_id     = os.environ.get("SERVICE_CLIENT_ID")   # skips config → form fields ignored
```

### 2.5 Field type rules

| Condition | `type` to use |
|-----------|---------------|
| Key contains: `secret`, `key`, `password`, `token`, `credential` | `"password"` |
| Multi-line content (e.g. JSON, private key) | `"textarea"` |
| Integer or float | `"number"` |
| True/False toggle | `"boolean"` |
| Fixed set of choices | `"select"` (add `"options": [{"value": "x", "label": "X"}]`) |
| Everything else | `"text"` |

### 2.6 Field suggestions — clickable chips — MANDATORY

**You MUST add a `suggestions` array to EVERY field where real values are known for this specific connector/provider.**
The goal is that the user never needs to open the provider's documentation — all useful values are shown as chips.

**Fields that MUST have suggestions (connector-specific real values required):**

| Field key pattern | `multi_select` | What to include |
|---|---|---|
| `scopes` | `true` | Every real OAuth scope string from this provider's docs — label + value (exact scope URL/string) + description of what it grants |
| `sync_query` / `filter` / `query` | `false` | All useful filter presets for this service (e.g. Gmail: `is:unread`, `in:inbox`, `has:attachment`, `from:me`, `label:important`, `newer_than:7d`) |
| `region` / `zone` | `false` | All real region codes the provider supports (e.g. AWS: `us-east-1`, `eu-west-1`, `ap-south-1`) |
| `api_version` | `false` | All supported versions (e.g. `v1`, `v2`, `v3`, `2024-01` — whatever this provider uses) |
| `pagination_type` | `false` | The pagination styles this provider uses (e.g. `cursor`, `offset`, `page_token`, `link_header`) |
| `environment` / `mode` | `false` | All real environments this provider has (e.g. `sandbox`, `production`, `staging`) |
| `language` / `locale` | `false` | Common language codes the service supports |
| `folder` / `mailbox` / `label` | `false` | Standard folder/label names for this service (e.g. Gmail: `INBOX`, `SENT`, `DRAFTS`, `SPAM`, `TRASH`) |
| `event_type` / `webhook_event` | `true` | All real event names this provider sends |
| `log_level` | `false` | `debug`, `info`, `warn`, `error` |

**Rules:**
- `multi_select: true` — clicking a chip appends/removes the value (space-separated). Use for `scopes`, `event_type`.
- `multi_select` absent / `false` — clicking replaces the entire field value. Use for single-choice fields.
- `description` in each suggestion is shown as a tooltip — make it useful (one short sentence about what the value does).
- Values MUST be the exact strings the provider API expects — do NOT paraphrase or invent.
- Do NOT add suggestions for: `client_id`, `client_secret`, `api_key`, `api_secret`, `password`, `token`, `private_key`, `service_account_json`, `username`.
- Suggestions must be SPECIFIC to this connector's provider — never use generic/placeholder examples.
- If you don't know the real values for a field, omit suggestions for that field rather than guessing.

**Example — Gmail `scopes` field:**
```json
{
  "key": "scopes",
  "type": "text",
  "multi_select": true,
  "suggestions": [
    {"label": "Read Only",   "value": "https://www.googleapis.com/auth/gmail.readonly", "description": "Read emails and metadata only"},
    {"label": "Read+Modify", "value": "https://www.googleapis.com/auth/gmail.modify",   "description": "Read, modify labels, and archive"},
    {"label": "Send",        "value": "https://www.googleapis.com/auth/gmail.send",     "description": "Send emails on behalf of the user"},
    {"label": "Full Access", "value": "https://mail.google.com/",                       "description": "Full Gmail access — read, send, delete, manage"},
    {"label": "Metadata",    "value": "https://www.googleapis.com/auth/gmail.metadata", "description": "Read email headers and metadata only, no body"}
  ]
}
```

**Example — Gmail `sync_query` field:**
```json
{
  "key": "sync_query",
  "type": "text",
  "suggestions": [
    {"label": "Unread",        "value": "is:unread",           "description": "Only unread emails"},
    {"label": "Inbox",         "value": "in:inbox",            "description": "Emails in the inbox folder"},
    {"label": "Has Attachment","value": "has:attachment",      "description": "Emails with file attachments"},
    {"label": "Sent by Me",    "value": "from:me",             "description": "Emails sent by the authenticated user"},
    {"label": "Important",     "value": "label:important",     "description": "Emails marked as important"},
    {"label": "Last 7 days",   "value": "newer_than:7d",       "description": "Emails received in the last 7 days"},
    {"label": "Last 30 days",  "value": "newer_than:30d",      "description": "Emails received in the last 30 days"},
    {"label": "Starred",       "value": "is:starred",          "description": "Starred emails only"},
    {"label": "No Attachment", "value": "has:no-attachment",   "description": "Emails without attachments"}
  ]
}
```

For Google OAuth scopes: `"value"` MUST be a real OAuth scope URL (e.g. `https://www.googleapis.com/auth/gmail.readonly`).
NEVER use a web UI URL (e.g. `https://gmail.com`) as a scope value.

### 2.7 required flag rules

- Set `required: true` if the install() method raises an error (returns MISSING_CREDENTIALS)
  when the key is absent.
- Set `required: false` for optional config (e.g. scopes with a default, sync_query filters).

### 2.8 Example: complete install_fields for an OAuth2 + scopes connector

```json
"install_fields": [
  {
    "key":         "client_id",
    "label":       "Client ID",
    "type":        "text",
    "required":    true,
    "placeholder": "your-client-id",
    "help":        "OAuth 2.0 Client ID from the developer console."
  },
  {
    "key":         "client_secret",
    "label":       "Client Secret",
    "type":        "password",
    "required":    true,
    "placeholder": "",
    "help":        "OAuth 2.0 Client Secret from the developer console."
  },
  {
    "key":         "scopes",
    "label":       "Scopes",
    "type":        "text",
    "required":    false,
    "placeholder": "https://www.googleapis.com/auth/gmail.readonly",
    "help":        "Space-separated OAuth scopes. Defaults to read/write if omitted.",
    "multi_select": true,
    "suggestions": [...]
  },
  {
    "key":         "sync_query",
    "label":       "Sync Filter Query",
    "type":        "text",
    "required":    false,
    "placeholder": "from:reports@example.com is:unread",
    "help":        "Optional filter query to scope which records are synced.",
    "suggestions": [...]
  }
]
```

---

## 3. apis array — Rules

- Must include **every public `async def` method** on the connector class.
- Always include: `install`, `authorize` (for oauth2), `health_check`, `sync`.
- Additional methods (e.g. `list_emails`, `send_email`, `delete_email`) must also be listed.
- API params use `"name"` (the Python parameter name) — NOT `"key"`.
- `returns` must be the actual Python return type. Use these exact values:

| Python return type | `"returns"` value |
|---|---|
| `ConnectorStatus` | `"ConnectorStatus"` |
| `TokenInfo` | `"TokenInfo"` |
| `SyncResult` | `"SyncResult"` |
| `list[dict]` / `List[...]` | `"list"` |
| `dict` / `Dict[...]` | `"object"` |
| `None` | `"null"` |
| `bool` | `"boolean"` |
| `str` | `"string"` |

### 3.1 `method` field = HTTP verb ONLY

The `"method"` field in each API entry is the **HTTP verb** (GET, POST, PUT, DELETE), NOT the Python method name.
The Python method name goes in the `"id"` field.

```
"id"     → Python method name (e.g. "sync", "list_emails", "send_email")
"method" → HTTP verb         (e.g. "POST", "GET")
```

HTTP verb selection:
- **GET**: health_check, list_*, get_*, fetch_* — any read-only operation
- **POST**: install, authorize, sync, send_*, create_*, delete_* (soft/trash), update_*

### 3.2 `authorize` method params

For `oauth2_code` flows, `authorize()` takes `auth_code: str` (not `auth_data: Dict`):

```json
{
  "id":          "authorize",
  "name":        "Authorize",
  "description": "Complete the OAuth2 authorization code exchange.",
  "method":      "POST",
  "params": [
    {"name": "auth_code", "type": "string",  "required": true},
    {"name": "state",     "type": "string",  "required": false}
  ],
  "returns": "TokenInfo"
}
```

### 3.3 Full example: sync method

```json
{
  "id":          "sync",
  "name":        "Sync",
  "description": "Synchronize records from the external service.",
  "method":      "POST",
  "params": [
    {"name": "since",       "type": "datetime", "required": false},
    {"name": "full",        "type": "boolean",  "required": false},
    {"name": "kb_id",       "type": "string",   "required": false},
    {"name": "webhook_url", "type": "string",   "required": false}
  ],
  "returns": "SyncResult"
}
```

---

## 4. painter section

`painter.config.title` = "Connect to {display_name}" (e.g. "Connect to Gmail").

`painter.config.fields` = **user-configurable runtime fields only** — this is NOT a verbatim copy of `install_fields`.

### What to include in painter.fields:
- Optional configuration fields the user can change after install: `scopes`, `sync_query`, `region`, `filters`, etc.

### What to EXCLUDE from painter.fields:
- Auth credentials: `client_id`, `client_secret`, `api_key`, `password` — these are install-time only
- System fields: `redirect_uri`, `access_token`, `refresh_token`

### Rule:
- If an install_field has `type: "password"` OR its key contains `secret`, `key`, `password`, `token`, `credential`, `id` → **EXCLUDE from painter.fields**
- If an install_field is a user-preference/filter (scopes, query, region, language, etc.) → **INCLUDE in painter.fields**

```json
// install_fields has: client_id, client_secret, scopes, sync_query
// painter.config.fields has: scopes, sync_query  ← auth credentials omitted
"painter": {
  "painter_type": "form",
  "config": {
    "title": "Connect to Gmail",
    "submit_label": "Connect",
    "fields": [
      { "key": "scopes", ... },
      { "key": "sync_query", ... }
    ]
  }
}
```

If there are no user-configurable fields (e.g. a pure API-key connector with no extra settings), `painter.config.fields` may be an empty array `[]`.

---

## 5. Version handling

- First generation: use version from step config, default `"1.0.0"`.
- Subsequent generations (re-running generate_metadata): bump the patch version
  (`1.0.0` → `1.0.1`, `1.0.1` → `1.0.2`, etc.).

---

## 6. connector_type

Must be the **exact string value** of the `CONNECTOR_TYPE` class attribute in `connector.py`.

```python
class YourConnector(BaseConnector):
    CONNECTOR_TYPE = "your_connector_type"   # ← use this exact value in connector.json
```

---

## 7. redirect_uri — NEVER in install_fields or config.py

`redirect_uri` must NEVER appear as an install_field.
The gateway stores it in `connector.config["redirect_uri"]` at runtime before calling `authorize()`.
In `connector.py`, always read it with: `redirect_uri = self.config.get("redirect_uri")`
In `config.py`, do NOT define `REDIRECT_URI` as a static value.

---

## 8. Auth Type Reference — Per-Auth Rules

The `AUTH_TYPE` class attribute in `connector.py` determines everything: what `auth_type` goes in connector.json,
which `install_fields` to include, whether `authorize` appears in `apis`, and what `painter.fields` contains.

### Master lookup table

| connector.py `AUTH_TYPE` | connector.json `"auth_type"` | Has `authorize` in apis? | install_fields required |
|---|---|---|---|
| `api_key` | `"api_key"` | ❌ No | `api_key` (password) |
| `bearer` | `"bearer_token"` | ❌ No | `token` or `api_key` (password) |
| `basic` | `"basic"` | ❌ No | `username` (text) + `password` (password) |
| `hmac` | `"api_key"` | ❌ No | `api_key` (text) + `api_secret` (password) |
| `aws_signature` / `aws_sigv4` | `"api_key"` | ❌ No | `access_key_id` (text) + `secret_access_key` (password) + `region` (text) |
| `oauth2_code` | `"oauth2"` | ✅ Yes | `client_id` (text) + `client_secret` (password) + `scopes` (text, optional) |
| `oauth2_pkce` | `"oauth2"` | ✅ Yes | `client_id` (text) — NO `client_secret` + `scopes` (text, optional) |
| `oauth2_client_credentials` | `"oauth2"` | ❌ No | `client_id` (text) + `client_secret` (password) |
| `oauth2_password` | `"oauth2"` | ❌ No | `client_id` (text) + `client_secret` (password) + `username` (text) + `password` (password) |
| `oauth2_device` | `"oauth2"` | ❌ No | `client_id` (text) + `client_secret` (password, optional) |
| `service_account` | `"service_account"` | ❌ No | `service_account_json` (textarea) |
| `jwt` | `"jwt"` | ❌ No | `private_key` (textarea) + `client_email` (text) + `token_uri` (text) |
| `none` | `"none"` | ❌ No | `[]` empty (or base_url if needed) |

---

### Auth type detail cards

#### `api_key`
```json
{
  "auth_type": "api_key",
  "install_fields": [
    {"key": "api_key", "label": "API Key", "type": "password", "required": true,
     "placeholder": "sk_live_...", "help": "Your secret API key from the developer dashboard."}
  ],
  "apis": [
    {"id": "install",      "method": "POST", "params": [{"name": "config", "type": "object", "required": true}], "returns": "ConnectorStatus"},
    {"id": "health_check", "method": "GET",  "params": [], "returns": "ConnectorStatus"},
    {"id": "sync",         "method": "POST", "params": [...sync_params...], "returns": "SyncResult"}
  ],
  "painter": {"config": {"fields": []}}
}
```
- No `authorize` in apis — gateway automatically injects api_key as a header.
- `painter.fields = []` — api_key is install-time only, nothing to configure at runtime.

---

#### `bearer`
```json
{
  "auth_type": "bearer_token",
  "install_fields": [
    {"key": "token", "label": "Access Token", "type": "password", "required": true,
     "placeholder": "ghp_...", "help": "Personal access token or pre-issued bearer token."}
  ],
  "apis": [install, health_check, sync, ...custom],
  "painter": {"config": {"fields": []}}
}
```
- No `authorize` — token is pre-issued and entered at install time.
- Use `"bearer_token"` (not `"bearer"`) as the connector.json `auth_type` value.

---

#### `basic`
```json
{
  "auth_type": "basic",
  "install_fields": [
    {"key": "username", "label": "Username", "type": "text",     "required": true, "placeholder": "your@email.com", "help": "Your account username or email."},
    {"key": "password", "label": "Password", "type": "password", "required": true, "placeholder": "",               "help": "Your account password."}
  ],
  "apis": [install, health_check, sync, ...custom],
  "painter": {"config": {"fields": []}}
}
```
- No `authorize` — gateway handles HTTP Basic Auth automatically.

---

#### `hmac`
```json
{
  "auth_type": "api_key",
  "install_fields": [
    {"key": "api_key",    "label": "API Key",    "type": "text",     "required": true},
    {"key": "api_secret", "label": "API Secret", "type": "password", "required": true}
  ],
  "apis": [install, health_check, sync, ...custom],
  "painter": {"config": {"fields": []}}
}
```
- Use `"api_key"` as the connector.json `auth_type` (HMAC is a signing mechanism, not a separate category).
- No `authorize`.

---

#### `aws_signature` / `aws_sigv4`
```json
{
  "auth_type": "api_key",
  "install_fields": [
    {"key": "access_key_id",     "label": "AWS Access Key ID",     "type": "text",     "required": true},
    {"key": "secret_access_key", "label": "AWS Secret Access Key", "type": "password", "required": true},
    {"key": "region",            "label": "AWS Region",            "type": "text",     "required": true,
     "placeholder": "us-east-1",
     "suggestions": [
       {"label": "US East (N. Virginia)", "value": "us-east-1"},
       {"label": "US West (Oregon)",      "value": "us-west-2"},
       {"label": "EU (Ireland)",          "value": "eu-west-1"},
       {"label": "AP (Singapore)",        "value": "ap-southeast-1"}
     ]}
  ],
  "painter": {"config": {"fields": [
    {"key": "region", ...}
  ]}}
}
```
- `painter.fields` may include `region` since it's user-configurable.
- No `authorize`.

---

#### `oauth2_code` (standard OAuth2 — Google, GitHub, Slack, HubSpot, etc.)
```json
{
  "auth_type": "oauth2",
  "install_fields": [
    {"key": "client_id",     "label": "Client ID",     "type": "text",     "required": true},
    {"key": "client_secret", "label": "Client Secret", "type": "password", "required": true},
    {"key": "scopes",        "label": "Scopes",        "type": "text",     "required": false,
     "multi_select": true, "suggestions": [...real OAuth scope URLs...]}
  ],
  "apis": [install, authorize, health_check, sync, ...custom],
  "painter": {"config": {"fields": [{"key": "scopes", ...}]}}
}
```
- **HAS `authorize` in apis** — user must click through OAuth popup.
- `painter.fields`: scopes and any user-configurable filters. NEVER client_id or client_secret.

---

#### `oauth2_pkce` (PKCE — no client_secret, e.g. mobile/SPA apps)
```json
{
  "auth_type": "oauth2",
  "install_fields": [
    {"key": "client_id", "label": "Client ID", "type": "text", "required": true},
    {"key": "scopes",    "label": "Scopes",    "type": "text", "required": false, "multi_select": true, "suggestions": [...]}
  ],
  "apis": [install, authorize, health_check, sync, ...custom],
  "painter": {"config": {"fields": [{"key": "scopes", ...}]}}
}
```
- **HAS `authorize`** — same popup flow as oauth2_code.
- **NO `client_secret`** in install_fields — PKCE uses code verifier instead.

---

#### `oauth2_client_credentials` (machine-to-machine — Stripe, Twilio, internal APIs)
```json
{
  "auth_type": "oauth2",
  "install_fields": [
    {"key": "client_id",     "label": "Client ID",     "type": "text",     "required": true},
    {"key": "client_secret", "label": "Client Secret", "type": "password", "required": true}
  ],
  "apis": [install, health_check, sync, ...custom],
  "painter": {"config": {"fields": []}}
}
```
- **NO `authorize`** — gateway calls `authorize_client_credentials()` automatically. No user popup.
- `painter.fields = []` — client_id and client_secret are install-time auth, not runtime config.

---

#### `oauth2_password` (Resource Owner Password Grant — legacy enterprise APIs)
```json
{
  "auth_type": "oauth2",
  "install_fields": [
    {"key": "client_id",     "label": "Client ID",     "type": "text",     "required": true},
    {"key": "client_secret", "label": "Client Secret", "type": "password", "required": true},
    {"key": "username",      "label": "Username",      "type": "text",     "required": true},
    {"key": "password",      "label": "Password",      "type": "password", "required": true}
  ],
  "apis": [install, health_check, sync, ...custom],
  "painter": {"config": {"fields": []}}
}
```
- **NO `authorize`** — gateway calls `authorize_password_grant()` automatically.

---

#### `service_account` (Google Service Account, GCP APIs)
```json
{
  "auth_type": "service_account",
  "install_fields": [
    {"key": "service_account_json", "label": "Service Account JSON", "type": "textarea", "required": true,
     "placeholder": "{\"type\": \"service_account\", \"project_id\": \"...\"}",
     "help": "Paste the full JSON content of your downloaded service account key file."}
  ],
  "apis": [install, health_check, sync, ...custom],
  "painter": {"config": {"fields": []}}
}
```
- **NO `authorize`** — gateway calls `authorize_service_account()` automatically.
- Use `"textarea"` type (not `"text"`) — the JSON key is multi-line.

---

#### `jwt` (JWT Bearer Assertion — RFC 7523)
```json
{
  "auth_type": "jwt",
  "install_fields": [
    {"key": "private_key",   "label": "Private Key (PEM)",   "type": "textarea", "required": true,
     "placeholder": "-----BEGIN RSA PRIVATE KEY-----\\n...", "help": "RSA private key in PEM format."},
    {"key": "client_email",  "label": "Client Email",         "type": "text",     "required": true,
     "placeholder": "service@project.iam.gserviceaccount.com"},
    {"key": "token_uri",     "label": "Token URI",            "type": "text",     "required": false,
     "placeholder": "https://oauth2.googleapis.com/token"}
  ],
  "apis": [install, health_check, sync, ...custom],
  "painter": {"config": {"fields": []}}
}
```
- **NO `authorize`** — gateway handles JWT assertion automatically.

---

#### `oauth2_device` (Device Code Flow — GitHub CLI, headless servers)
```json
{
  "auth_type": "oauth2",
  "install_fields": [
    {"key": "client_id", "label": "Client ID", "type": "text", "required": true},
    {"key": "scopes",    "label": "Scopes",    "type": "text", "required": false, "multi_select": true}
  ],
  "apis": [install, health_check, sync, ...custom],
  "painter": {"config": {"fields": [{"key": "scopes", ...}]}}
}
```
- **NO `authorize`** — gateway handles device code poll automatically.
- No `client_secret` (device flow is public client).

---

#### `none` (No authentication)
```json
{
  "auth_type": "none",
  "install_fields": [],
  "apis": [install, health_check, sync, ...custom],
  "painter": {"config": {"fields": []}}
}
```
- No install_fields (unless the service needs a custom base_url or similar).
- No `authorize`.
- `install` may still be included for initial setup validation even with no auth.

---

### Critical rule summary for apis list

| Auth type | Include `authorize` in apis? |
|---|---|
| `oauth2_code`, `oauth2_pkce` | ✅ YES — user must click through OAuth popup |
| `oauth2_client_credentials`, `oauth2_password`, `oauth2_device` | ❌ NO — gateway handles automatically |
| `api_key`, `bearer`, `basic`, `hmac`, `aws_signature` | ❌ NO — credentials entered at install, no flow needed |
| `service_account`, `jwt` | ❌ NO — gateway calls authorize_service_account / _authorize_jwt_assertion |
| `none` | ❌ NO |

---

## 9. Output format

Return ONLY a valid JSON object.
- No markdown code fences
- No prose or explanation
- No trailing commas
- All strings properly escaped
- name: ALWAYS Shielva {service_name} Connector (this line is the upgrade sentinel — do not remove)
"""

# ── Helpers (mirrored from guidelines_service.py) ────────────────────

def _get_r2():
    from integration.services import r2_service
    return r2_service

async def _get_redis():
    import redis.asyncio as aioredis
    url = settings.REDIS_URL
    return await aioredis.from_url(url, encoding="utf-8", decode_responses=True)

def _metadata_collection():
    from integration.db.database import get_db
    return get_db()["metadata_writing_guidelines"]

async def _r2_put_text(r2, key: str, content: str) -> None:
    try:
        if r2._use_local():
            lp = r2._local_path(key)
            lp.parent.mkdir(parents=True, exist_ok=True)
            lp.write_text(content, encoding="utf-8")
        else:
            loop = asyncio.get_event_loop()
            client = r2._get_client()
            bucket = settings.R2_BUCKET_NAME
            await loop.run_in_executor(
                None, partial(r2._sync_write, client, bucket, key, content, "text/plain")
            )
    except Exception as exc:
        logger.warning("metadata_guidelines.r2_write_failed", key=key, error=str(exc))

async def _ingest_to_rag(content: str, title: str) -> None:
    """Ingest metadata writing guideline into MCP RAG (global KB)."""
    try:
        from integration.services import knowledge_service
        await knowledge_service._ingest_to_mcp(
            content=content,
            title=title,
            kb_id="codegen-guidelines-global",
            tenant_id="__global__",
            doc_id="guidelines_metadata_global",
        )
        logger.info("metadata_guidelines.rag_ingested", title=title)
    except Exception as exc:
        logger.warning("metadata_guidelines.rag_ingest_failed", error=str(exc))

# ── Public API ────────────────────────────────────────────────────────

async def seed_metadata_writing_guidelines() -> None:
    """Seed / upgrade metadata_writing_guideline.md on startup.

    - First boot: creates v1.0.0 in MongoDB + R2 + RAG.
    - Subsequent boots: if active record is missing the sentinel string,
      auto-upgrades to the latest default so Gemini always has current rules.
    """
    try:
        col = _metadata_collection()
        active = await col.find_one({"is_active": True}, sort=[("created_at", -1)])

        if active:
            if _SENTINEL in active.get("content", ""):
                logger.info("metadata_guidelines.seed_skipped",
                            reason="already_up_to_date", version=active.get("version"))
                return
            # Auto-upgrade — missing sentinel means stale version
            logger.info("metadata_guidelines.seed_upgrading",
                        from_version=active.get("version"),
                        reason="missing name/redirect_uri/method-verb sentinel")
            await _save(DEFAULT_METADATA_WRITING_MD,
                        change_description="Auto-upgrade: added 'name' field (Shielva X Connector convention), "
                                           "fixed 'method' to HTTP verb, added redirect_uri exclusion rule")
            return

        # First boot
        now = datetime.now(timezone.utc)
        await col.insert_one({
            "version": "1.0.0",
            "content": DEFAULT_METADATA_WRITING_MD,
            "change_description": "Initial default — OAuth2 credentials, install_fields, painter rules",
            "created_at": now,
            "is_active": True,
        })
        logger.info("metadata_guidelines.seed_mongodb", version="1.0.0")

        r2 = _get_r2()
        await _r2_put_text(r2, f"{_R2_PREFIX}/{_STANDARD_KEY}", DEFAULT_METADATA_WRITING_MD)
        await _r2_put_text(r2, f"{_R2_PREFIX}/{_VERSIONED_KEY.format(version='1.0.0')}", DEFAULT_METADATA_WRITING_MD)
        logger.info("metadata_guidelines.seed_r2")

        await _ingest_to_rag(DEFAULT_METADATA_WRITING_MD, "Metadata Writing Guidelines v1.0.0")
        logger.info("metadata_guidelines.seed_complete", version="1.0.0")

    except Exception as exc:
        logger.error("metadata_guidelines.seed_failed", error=str(exc))


async def _save(content: str, change_description: str = "") -> Dict[str, Any]:
    """Save new version to MongoDB + R2 + Redis + RAG."""
    col = _metadata_collection()
    prev = await col.find_one({"is_active": True}, sort=[("created_at", -1)])
    prev_version = prev["version"] if prev else "1.0.0"

    # Bump patch version
    parts = prev_version.split(".")
    if len(parts) == 3:
        parts[2] = str(int(parts[2]) + 1)
        new_version = ".".join(parts)
    else:
        new_version = "1.0.1"

    now = datetime.now(timezone.utc)
    await col.update_many({"is_active": True}, {"$set": {"is_active": False}})
    await col.insert_one({
        "version": new_version,
        "content": content,
        "change_description": change_description,
        "created_at": now,
        "is_active": True,
    })

    r2 = _get_r2()
    await _r2_put_text(r2, f"{_R2_PREFIX}/{_STANDARD_KEY}", content)
    await _r2_put_text(r2, f"{_R2_PREFIX}/{_VERSIONED_KEY.format(version=new_version)}", content)

    try:
        r = await _get_redis()
        await r.setex(_REDIS_KEY_TPL.format(version=new_version), 3600, content)
        await r.aclose()
    except Exception as exc:
        logger.warning("metadata_guidelines.redis_failed", error=str(exc))

    await _ingest_to_rag(content, f"Metadata Writing Guidelines v{new_version}")
    logger.info("metadata_guidelines.saved", version=new_version)
    return {"version": new_version, "content": content, "updated_at": str(now)}


async def save_guidelines(content: str, change_description: str = "") -> Dict[str, Any]:
    """Public API — save a new version of the metadata writing guideline."""
    return await _save(content, change_description)


async def get_active_guidelines() -> Dict[str, Any]:
    """Return active guideline: {version, content, updated_at}."""
    try:
        col = _metadata_collection()
        doc = await col.find_one({"is_active": True}, sort=[("created_at", -1)])
        if not doc:
            return {"version": "1.0.0", "content": DEFAULT_METADATA_WRITING_MD, "updated_at": ""}

        # Try Redis
        try:
            r = await _get_redis()
            cached = await r.get(_REDIS_KEY_TPL.format(version=doc["version"]))
            await r.aclose()
            if cached:
                return {"version": doc["version"], "content": cached,
                        "updated_at": str(doc.get("created_at", ""))}
        except Exception:
            pass

        content = doc.get("content", DEFAULT_METADATA_WRITING_MD)
        return {"version": doc["version"], "content": content,
                "updated_at": str(doc.get("created_at", ""))}
    except Exception as exc:
        logger.error("metadata_guidelines.get_failed", error=str(exc))
        return {"version": "1.0.0", "content": DEFAULT_METADATA_WRITING_MD, "updated_at": ""}


async def get_version_history() -> List[Dict[str, Any]]:
    """Return all versions, newest first."""
    try:
        col = _metadata_collection()
        cursor = col.find({}, {"_id": 0, "content": 0}).sort("created_at", -1).limit(50)
        docs = await cursor.to_list(length=50)
        return [
            {
                "version": d.get("version"),
                "change_description": d.get("change_description", ""),
                "created_at": str(d.get("created_at", "")),
                "is_active": d.get("is_active", False),
            }
            for d in docs
        ]
    except Exception as exc:
        logger.error("metadata_guidelines.history_failed", error=str(exc))
        return []
