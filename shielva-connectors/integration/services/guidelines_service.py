"""Integration Builder — CODE_EXECUTION_GUIDELINES service.

Manages connector_development.md: the standard coding practices document.
Storage hierarchy: Redis cache → R2 bucket → embedded default.
Every save creates a new MongoDB version record.
"""

import asyncio
import json
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from integration.core.config import settings

logger = structlog.get_logger(__name__)

# R2 key prefix for guidelines — baked at import time (same pattern as docs_guidelines_service)
# Full path in bucket: {R2_COLLECTION_PREFIX}/CODE_EXECUTION_GUIDELINES/...
# e.g. shielvasense-integration-plans/CODE_EXECUTION_GUIDELINES/connector_development.md
_GUIDELINES_R2_PREFIX = f"{settings.R2_COLLECTION_PREFIX}/CODE_EXECUTION_GUIDELINES"
_STANDARD_KEY = "connector_development.md"
_VERSIONED_KEY_TPL = "connector_v_{version}.md"

# Redis cache key template
_REDIS_KEY_TPL = "connector_guidelines:v{version}"
_ACTIVE_VERSION_KEY = "connector_guidelines:active_version"

# ── Default connector_development.md ─────────────────────────────────

DEFAULT_CONNECTOR_DEVELOPMENT_MD = """# Shielva Connector Development Standard

> Managed by Shielva Integration Builder • CODE_EXECUTION_GUIDELINES

---

## Design Principles

These two principles are **non-negotiable** and apply to every file in every connector package.

### Open / Closed Principle (OCP)

> *"Software entities should be open for extension, but closed for modification."*

Every connector module is **closed for modification** once it ships — you extend it, never edit it.

**What this means in practice:**

| Rule | Correct ✅ | Wrong ❌ |
|---|---|---|
| New auth flow needed | Add `helpers/oauth2_pkce.py`, call from connector | Edit existing `auth.py` |
| New sync strategy | Add `helpers/incremental_sync.py` | Edit `connector.py::sync()` |
| New API version | Subclass the connector, override affected methods | Patch `http_client.py` for the new URL |
| New config field | Add field with a default value to `config.py` | Remove or rename an existing field |
| New normalizer logic | Add a new method to `normalizer.py` | Change existing `normalize_*` signatures |

**Enforcement rules:**
1. `BaseConnector` abstract methods (`install`, `authorize`, `health_check`, `sync`) are **frozen** — their signatures never change after first release
2. New capabilities must be added as **new files or new methods**, never by editing existing ones
3. `client/http_client.py` exposes a stable interface — callers never break when internals change
4. Exceptions in `exceptions.py` are additive only — never remove or rename a raised exception class

---

### Separation of Concerns (SoC)

> *"Each module has exactly one reason to change."*

Every file owns one responsibility. **Never let concerns bleed across module boundaries.**

| Module | Owns | Must NEVER |
|---|---|---|
| `connector.py` | Lifecycle orchestration (`install` → `authorize` → `health_check` → `sync`); token storage via `self.set_token()` / `self.get_token()` | Contain HTTP logic or data transformation |
| `client/http_client.py` | HTTP transport: retry, rate-limiting, base headers, timeouts | Know about business logic, service models, or tenant data |
| `helpers/normalizer.py` | Transform raw API response dicts → `NormalizedDocument` | Make HTTP calls, import `http_client`, or store any state |
| `helpers/utils.py` | Pure utility functions (date formatting, pagination math, string helpers) | Import from `connector.py` or `http_client.py` |
| `models.py` | Pydantic schemas for API request/response payloads | Contain business logic, call methods, or import connector classes |
| `exceptions.py` | Custom exception class hierarchy | Import from any other module in the package |
| `config.py` | `pydantic-settings` configuration loaded from environment | Contain logic, defaults that require network, or mutable state |

**Enforcement rules:**
1. `connector.py` must only import from: `shared.base_connector`, `.client.http_client`, `.helpers.normalizer`, `.config`, `.exceptions`
2. `http_client.py` must only import from: `httpx`, `.exceptions`, standard library
3. `normalizer.py` must only import from: `shared.base_connector` (for `NormalizedDocument`), `.models`, `.utils`, standard library
4. No circular imports — ever. If you need it, refactor into a shared `utils.py`

### SRP-A — connector.py delegates ALL API calls through the client (NEVER calls SDK directly)

`connector.py` must **never** instantiate or call the underlying SDK/transport directly.
Every outbound API call must go through the named methods on the HTTP client class.

❌ Wrong — `connector.py` bypassing the client:
```python
# connector.py
service = build('gmail', 'v1', credentials=creds)
results = service.users().messages().list(userId="me").execute()
```

✅ Correct — `connector.py` calls through the client:
```python
# connector.py
client = await self._get_client()      # returns GmailClient instance
results = await client.list_messages("me", maxResults=100)
```

`_get_client()` builds and caches the client instance. `connector.py` never calls `build()`,
`.execute()`, `httpx.get()`, `requests.post()`, or any SDK transport method directly.

### SRP-B — helpers/ payload builders return the FINAL encoded form

Any helper function that constructs a request payload must return the **fully serialized value**
ready to pass straight to the client. `connector.py` must never do base64 encoding, JSON-dumping,
or MIME assembly inline.

❌ Wrong — encoding split across connector.py and helper:
```python
# helpers/utils.py
def build_email_message(recipient, subject, body):
    msg = MIMEMultipart()
    ...
    return msg   # returns raw object — connector must encode it

# connector.py
raw_msg = build_email_message(recipient, subject, body)
body = {"raw": base64.urlsafe_b64encode(raw_msg.as_bytes()).decode()}  # ← encoding leaked into connector
```

✅ Correct — helper returns the final encoded string:
```python
# helpers/utils.py
def build_email_message(recipient, subject, body) -> str:
    msg = MIMEMultipart()
    msg["to"] = recipient; msg["subject"] = subject
    msg.attach(MIMEText(body))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()  # returns ready-to-use str

# connector.py
raw_b64 = build_email_message(recipient, subject, body)
await client.send_message("me", {"raw": raw_b64})   # no encoding here
```

---

## Package Structure

Every connector MUST use this directory layout:

```
{service}_connector/
├── __init__.py              # Package exports
├── connector.py             # Main connector class (extends BaseConnector)
├── config.py                # Configuration (pydantic-settings BaseSettings)
├── models.py                # Pydantic models for API request/response schemas
├── exceptions.py            # Custom exception classes
├── helpers/                 # Utility modules
│   ├── __init__.py
│   ├── utils.py             # Shared utilities, date formatters, pagination helpers
│   └── normalizer.py        # Transform raw API responses → NormalizedDocument
├── client/                  # HTTP client layer
│   ├── __init__.py
│   └── http_client.py       # Async HTTP client with retry + rate limiting
└── tests/
    ├── __init__.py
    ├── test_connector.py    # Unit tests for all connector methods
    └── test_auth.py         # Unit tests for OAuth2/auth flow
```

---

## Core Rules

1. **Inherit from BaseConnector** — never implement standalone connectors
2. **Multi-tenant** — all data and doc_ids scoped by `self.tenant_id`
3. **Token storage** — use `self.set_token()` / `self.get_token()` directly in `connector.py`; tokens are automatically persisted to Redis by BaseConnector. NEVER store tokens in files, instance variables, or MongoDB.
4. **HTTP client** — use `client/http_client.py` for ALL external HTTP calls; never import `httpx` directly in `connector.py`
5. **Helpers** — use `helpers/utils.py` for shared utilities; `helpers/normalizer.py` for data transformation
6. **No secrets** — NEVER hardcode API keys, tokens, tenant IDs, or environment-specific values
7. **No redirect_uri in config.py** — `redirect_uri` is injected by the gateway at runtime into `connector.config` before `authorize()` is called. In `authorize()` always read it with `redirect_uri = self.config.get("redirect_uri")`. NEVER define `REDIRECT_URI` as a static value in `config.py`.
8. **Async first** — all I/O methods must be `async def`
9. **Logging** — use `structlog` for all logging; never use `print()`
9. **Error handling** — wrap all external calls in try/except with meaningful error messages

---

## Import Rules

```python
from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth,
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus,
)
```

### Subpackage imports in connector.py — BARE module name ONLY (NEVER package-prefixed):
The gateway adds the connector's own directory to sys.path. connector.py must use bare imports:
  ✅ `from client.http_client import GmailClient`
  ✅ `from helpers import gmail_utils`
  ✅ `from helpers.gmail_utils import build_raw_email_message`
  ❌ `from gmail_connector.client.http_client import GmailClient`   ← WRONG — package-prefixed
  ❌ `from .client.http_client import GmailClient`                  ← WRONG — relative import

`__init__.py` files inside subpackages (client/, helpers/) MAY use relative imports:
  ✅ `client/__init__.py`: `from .http_client import GmailClient`
  ✅ `helpers/__init__.py`: `from .gmail_utils import build_raw_email_message`

---

## Token Storage

Tokens are stored via `self.set_token(token_info)` and read via `self.get_token()` directly in `connector.py`.
BaseConnector automatically persists tokens to Redis — no additional storage layer needed.
Store: access_token, refresh_token, expires_at, scopes, and any service-specific attributes as key-value pairs in `TokenInfo`.

```python
# In connector.py — call directly, no token_store wrapper needed
token_info = TokenInfo(
    access_token=data["access_token"],
    refresh_token=data.get("refresh_token"),
    expires_at=datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600)),
    scope=data.get("scope", ""),
)
await self.set_token(token_info)

# Reading tokens
token = await self.get_token()
if token:
    access_token = token.access_token
```

---

## Client Layer

`client/http_client.py` must implement:
- **Retry** with exponential backoff (max 3 attempts, base delay 1s)
- **Rate limiting** — respect `Retry-After` headers
- **Base headers** — auth (Bearer token from TokenStore), Content-Type
- **Timeout** — 60s default, configurable

### OCP-3 — Retry/backoff timing must be class constants (never hardcoded literals)

❌ Wrong — hardcoded, impossible to tune without a code change:
```python
await asyncio.sleep(2 ** attempt)
```

✅ Correct — exposed as class constants so subclasses or operators can override:
```python
class MyHttpClient:
    MAX_RETRIES       = 3
    INITIAL_BACKOFF_S = 1.0   # OCP: tune retry timing here, never inside the retry loop
    BACKOFF_FACTOR    = 2.0   # OCP: tune backoff multiplier here

    async def _execute(self, ...):
        for attempt in range(self.MAX_RETRIES):
            ...
            backoff = self.INITIAL_BACKOFF_S * (self.BACKOFF_FACTOR ** attempt)
            await asyncio.sleep(backoff)
```

---

## TDD Approach — CRITICAL

Tests are the SPECIFICATION. Tests define what the connector MUST do.
If tests fail, **fix the connector** — never change test assertions.
- `test_connector.py` tests: install, authorize, health_check, sync
- Each `doc_id` must include `tenant_id` for multi-tenant isolation
- Mock all external HTTP calls — never make real API calls in tests

---

## Python Docstrings — REQUIRED IN EVERY FILE

Every `.py` file in the connector package **must** have docstrings at three levels: module, class, and method.
This is not optional — the LLM must emit docstrings for every file it generates.

### 1. Module-level docstring (top of every `.py` file)

The very first statement in each file must be a triple-quoted module docstring describing the file's responsibility.

```python
'''Google AdSense - HTTP client layer.

Handles all outbound HTTP calls to the AdSense Management API v1.4.
Implements retry with exponential backoff, rate-limit respect, and
OAuth2 Bearer token injection.

Belongs to: client/http_client.py
Package:    adsense_connector
'''
```

| File | What the module docstring must say |
|---|---|
| `connector.py` | Main connector class, lifecycle methods it implements, service name |
| `config.py` | What settings it exposes, where they come from (env vars) |
| `models.py` | What API schemas/models are defined here |
| `exceptions.py` | Exception hierarchy overview |
| `helpers/utils.py` | What utility functions live here |
| `helpers/normalizer.py` | What it transforms (raw API dict → NormalizedDocument) |
| `client/http_client.py` | HTTP transport responsibilities (retry, auth headers, timeouts) |
| `client/http_client.py` (class docstring) | HTTP transport responsibilities (retry, auth headers, timeouts) |
| `tests/test_connector.py` | What is being tested and the mocking strategy |
| `__init__.py` (each) | What is exported from this package/subpackage |

---

### 2. Class-level docstring

Every class must have a docstring immediately after the `class` declaration:

```python
class AdsenseHttpClient:
    '''Async HTTP client for the Google AdSense Management API v1.4.

    Wraps httpx.AsyncClient with:
    - Automatic OAuth2 Bearer token injection via TokenStore
    - Exponential backoff retry (max 3 attempts, base 1 s)
    - Respect for Retry-After response headers
    - Configurable request timeout (default 60 s)

    Args:
        connector: BaseConnector instance used to fetch the current access token via get_token().
        config: AdsenseConfig with base URL and timeout settings.
    '''
```

---

### 3. Method-level docstring (every public `def` / `async def`)

Use Google-style docstrings with Args, Returns, and Raises sections:

```python
async def sync(
    self,
    cursor: Optional[str] = None,
    limit: int = 100,
) -> SyncResult:
    '''Fetch AdSense reports since the last sync cursor.

    Calls the AdSense Reports API, normalises each row into a
    NormalizedDocument, and persists the next-page token for
    incremental sync.

    Args:
        cursor: Opaque pagination token from a previous sync result.
                Pass None to start from the beginning.
        limit:  Maximum number of records to return per page (1-1000).

    Returns:
        SyncResult with documents, next cursor, and sync status.

    Raises:
        AdsenseAuthError: If the access token is expired and refresh fails.
        AdsenseRateLimitError: If the API returns 429 after all retries.
        AdsenseAPIError: For any other non-2xx API response.
    '''
```

**Rules:**
1. Every `async def` and `def` that is not a `@property` getter must have a docstring
2. `__init__` must document its parameters in the class docstring (not a separate `__init__` docstring)
3. Private helpers (prefix `_`) need at minimum a one-line summary docstring
4. `@staticmethod` and `@classmethod` follow the same rules

---

### 4. `__init__.py` docstring + `__all__`

```python
'''AdSense connector package.

Exports:
    AdsenseConnector - main connector class, extends BaseConnector.
'''

from .connector import AdsenseConnector

__all__ = ["AdsenseConnector"]
```

Every `__init__.py` must:
- Have a module docstring listing what it exports
- Declare `__all__` with every public name

---

## Authentication Types

Every connector class must set `AUTH_TYPE` matching one of the values below.
The auth type determines which methods to implement and how credentials flow.

| `auth_type` | Flow | `authorize()` needed? |
|---|---|---|
| `oauth2_code` | OAuth2 Authorization Code | ✅ Yes |
| `oauth2_pkce` | OAuth2 + PKCE challenge | ✅ Yes |
| `oauth2_client_credentials` | OAuth2 Client Credentials (server-to-server) | ❌ No |
| `api_key` | Static key in header/query | ❌ No |
| `service_account` | JWT-signed service account JSON | ❌ No |
| `basic_auth` | Base64 username:password header | ❌ No |

### oauth2_code and oauth2_pkce

```python
AUTH_URI  = "https://provider.com/oauth/authorize"   # REQUIRED — must be a real URL
TOKEN_URI = "https://provider.com/oauth/token"        # REQUIRED — must be a real URL
SCOPES    = ["scope1", "scope2"]                      # minimum scopes needed

async def install(self) -> ConnectorStatus:
    return ConnectorStatus(..., health=ConnectorHealth.OFFLINE, auth_status=AuthStatus.PENDING,
                           message="Click Authorize to connect")

async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
    # Exchange code for tokens, call self.set_token(token_info), return token_info
    ...
```

- `install()` always returns `PENDING` — the user must click Authorize in the UI
- `authorize()` exchanges the code, calls `self.set_token()`, returns `TokenInfo`
- Call `self.ensure_token()` before every API call — it auto-refreshes expired tokens
- NEVER store `client_secret` outside of `self.config`

### oauth2_client_credentials

```python
TOKEN_URI = "https://provider.com/oauth/token"   # REQUIRED

async def install(self) -> ConnectorStatus:
    # POST to TOKEN_URI with grant_type=client_credentials, store via self.set_token()
    # Return CONNECTED on success, FAILED on error
    ...
```

- No `authorize()` method — there is no redirect flow
- `install()` fetches the token directly and stores it with `self.set_token()`
- `health_check()` calls `self.ensure_token()` to handle expiry

### api_key

```python
async def install(self) -> ConnectorStatus:
    api_key = self.config.get("api_key", "")
    if not api_key:
        return ConnectorStatus(..., auth_status=AuthStatus.MISSING_CREDENTIALS)
    # Validate with a lightweight API call (e.g. GET /me)
    # Return CONNECTED on success
    ...
```

- No `authorize()`, no `set_token()`, no token refresh
- Inject key in every request: `headers = {"Authorization": f"Bearer {self.config['api_key']}"}`
- `install_fields` must include `api_key` (type="password")

### service_account

```python
async def install(self) -> ConnectorStatus:
    import json, jwt
    sa = json.loads(self.config.get("service_account_json", "{}"))
    # Build JWT assertion, POST to TOKEN_URI, store short-lived token via self.set_token()
    ...
```

- `install_fields` must include `service_account_json` (type="textarea", required=true)
- Short-lived access token stored via `self.set_token()` with `expires_at`
- Check `token.is_expired()` before every sync; refresh by minting a new JWT if expired
- NEVER log or store the private key beyond the current request

### basic_auth

```python
import base64

async def install(self) -> ConnectorStatus:
    username = self.config.get("username", "")
    password = self.config.get("password", "")
    if not username or not password:
        return ConnectorStatus(..., auth_status=AuthStatus.MISSING_CREDENTIALS)
    ...

def _auth_headers(self) -> dict:
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}
```

- No `authorize()`, no token refresh
- `install_fields` must include `username` (type="text") and `password` (type="password")

---

## Code Quality

- Full type hints on all public methods and function signatures
- Docstrings at module, class, and method level in **every** `.py` file (see Python Docstrings section above)
- `__all__` in every `__init__.py`
- Line length: 100 characters max
- No bare `except:` — always catch specific exception types

---

## Connector Metadata — `metadata/connector.json` (MANDATORY)

Every connector package **MUST** include a `metadata/connector.json` file. This file is the single source of truth for:
1. **Install form** — fields shown in the CMS UI when deploying the connector
2. **API catalogue** — all callable methods with their parameter schemas
3. **Painter config** — runtime configuration form for the connector

### Location
```
{service}_connector/
└── metadata/
    └── connector.json
```

### Required Schema

```json
{
  "connector_type": "shielva_aws_connector",
  "name": "Shielva AWS Connector",
  "display_name": "AWS",
  "version": "1.0.0",
  "description": "Short description of what this connector does",
  "auth_type": "api_key",
  "install_fields": [
    {
      "key": "access_key_id",
      "label": "AWS Access Key ID",
      "type": "text",
      "required": true,
      "placeholder": "AKIA...",
      "help": "Your AWS IAM Access Key ID."
    },
    {
      "key": "secret_access_key",
      "label": "AWS Secret Access Key",
      "type": "password",
      "required": true,
      "placeholder": "",
      "help": "Your AWS IAM Secret Access Key."
    },
    {
      "key": "region",
      "label": "AWS Region",
      "type": "text",
      "required": true,
      "placeholder": "us-east-1",
      "help": "AWS region where your resources are hosted.",
      "suggestions": [
        {"label": "US East (N. Virginia)", "value": "us-east-1"},
        {"label": "US West (Oregon)",      "value": "us-west-2"},
        {"label": "EU (Ireland)",          "value": "eu-west-1"}
      ]
    }
  ],
  "apis": [
    {
      "id": "install",
      "name": "Install",
      "description": "Install and configure the connector.",
      "method": "POST",
      "params": [{"name": "config", "type": "object", "required": true}],
      "returns": "ConnectorStatus"
    },
    {
      "id": "health_check",
      "name": "Health Check",
      "description": "Verify connector connectivity and credentials.",
      "method": "GET",
      "params": [],
      "returns": "ConnectorStatus"
    },
    {
      "id": "sync",
      "name": "Sync",
      "description": "Fetch and ingest documents into the knowledge base.",
      "method": "POST",
      "params": [
        {"name": "since",       "type": "datetime", "required": false},
        {"name": "full",        "type": "boolean",  "required": false},
        {"name": "kb_id",       "type": "string",   "required": false},
        {"name": "webhook_url", "type": "string",   "required": false}
      ],
      "returns": "SyncResult"
    }
  ],
  "painter": {
    "painter_type": "form",
    "config": {
      "title": "Connect to AWS",
      "submit_label": "Connect",
      "fields": [
        {"key": "region", "label": "AWS Region", "type": "text", "required": true, "placeholder": "us-east-1", "suggestions": [...]}
      ]
    }
  }
}
```

### Key rules for `connector.json`
1. `connector_type` = exact `CONNECTOR_TYPE` class attribute value in `connector.py`
2. `name` = `"Shielva {ServiceName} Connector"` — ALWAYS this pattern
3. `display_name` = short service name only (e.g. `"AWS"`, `"Gmail"`, `"Slack"`)
4. `install_fields` — only fields the user enters at deploy time. NEVER include `redirect_uri`.
5. API params use `"name"` (not `"key"`). `method` = HTTP verb (GET/POST), not the Python method name.
6. `painter.config.fields` = user-configurable runtime fields ONLY. Exclude auth credentials (`client_id`, `client_secret`, `api_key`, any `type="password"` field). If none exist, use `[]`.
7. `version` starts at `"1.0.0"`, bumped as patch on each rebuild.

---

## Auth Type — connector.py Implementation Patterns

**Read the `AUTH_TYPE` from `connector.json` → apply the corresponding implementation pattern below.**

| AUTH_TYPE | connector.json `auth_type` | Implement `authorize()`? | Core install_fields |
|---|---|---|---|
| `api_key` | `"api_key"` | ❌ No | `api_key` (password) |
| `bearer` | `"bearer_token"` | ❌ No | `token` (password) |
| `basic` | `"basic"` | ❌ No | `username` (text) + `password` (password) |
| `hmac` | `"api_key"` | ❌ No | `api_key` (text) + `api_secret` (password) |
| `aws_signature`/`aws_sigv4` | `"api_key"` | ❌ No | `access_key_id` + `secret_access_key` + `region` |
| `oauth2_code` | `"oauth2"` | ✅ YES | `client_id` + `client_secret` + optional `scopes` |
| `oauth2_pkce` | `"oauth2"` | ✅ YES | `client_id` only (no secret) + optional `scopes` |
| `oauth2_client_credentials` | `"oauth2"` | ❌ No | `client_id` + `client_secret` |
| `oauth2_password` | `"oauth2"` | ❌ No | `client_id` + `client_secret` + `username` + `password` |
| `oauth2_device` | `"oauth2"` | ❌ No | `client_id` + optional `scopes` |
| `service_account` | `"service_account"` | ❌ No | `service_account_json` (textarea) |
| `jwt` | `"jwt"` | ❌ No | `private_key` (textarea) + `client_email` + `token_uri` |
| `none` | `"none"` | ❌ No | `[]` empty |

### Pattern: `api_key` / `bearer` / `basic`
```python
class MyConnector(BaseConnector):
    CONNECTOR_TYPE = "shielva_my_connector"
    AUTH_TYPE = "api_key"   # or "bearer" or "basic"

    def __init__(self, tenant_id, connector_id, config=None):
        super().__init__(tenant_id, connector_id, config)
        cfg = config or {}
        self.api_key = cfg.get("api_key") or os.environ.get("SERVICE_API_KEY")

    async def install(self, config):
        if not self.api_key:
            return ConnectorStatus(connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY, auth_status=AuthStatus.MISSING_CREDENTIALS,
                error="API key is required.")
        await self.save_config(config)
        return ConnectorStatus(connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)

    # NO authorize() method — gateway injects credentials automatically
```

### Pattern: `oauth2_code` / `oauth2_pkce`
```python
class MyConnector(BaseConnector):
    CONNECTOR_TYPE = "shielva_my_connector"
    AUTH_TYPE = "oauth2_code"
    AUTH_URI  = "https://provider.com/oauth2/auth"
    TOKEN_URI = "https://provider.com/oauth2/token"
    REQUIRED_SCOPES = ["scope.read", "scope.write"]

    def __init__(self, tenant_id, connector_id, config=None):
        super().__init__(tenant_id, connector_id, config)
        cfg = config or {}
        self.client_id     = cfg.get("client_id")     or os.environ.get("SERVICE_CLIENT_ID")
        self.client_secret = cfg.get("client_secret") or os.environ.get("SERVICE_CLIENT_SECRET")
        # oauth2_pkce: no client_secret

    async def install(self, config):
        if not self.client_id or not self.client_secret:
            return ConnectorStatus(connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY, auth_status=AuthStatus.MISSING_CREDENTIALS)
        await self.save_config(config)
        return ConnectorStatus(connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.PENDING,
            message="Authorization required — click Authorize to continue.")

    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        redirect_uri = self.config.get("redirect_uri")  # ALWAYS from self.config, never hardcoded
        if not auth_code or not redirect_uri:
            raise ValueError("auth_code and redirect_uri are required.")
        # exchange code for tokens, call set_token(), return TokenInfo
```

### Pattern: `oauth2_client_credentials`
```python
class MyConnector(BaseConnector):
    CONNECTOR_TYPE = "shielva_my_connector"
    AUTH_TYPE = "oauth2_client_credentials"
    TOKEN_URI = "https://provider.com/oauth2/token"

    # NO authorize() — gateway calls authorize_client_credentials() automatically
    # install() validates client_id + client_secret and saves config
```

### Pattern: `service_account`
```python
class MyConnector(BaseConnector):
    CONNECTOR_TYPE = "shielva_my_connector"
    AUTH_TYPE = "service_account"

    async def install(self, config):
        sa_json = config.get("service_account_json")
        if not sa_json:
            return ConnectorStatus(connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY, auth_status=AuthStatus.MISSING_CREDENTIALS,
                error="Service account JSON is required.")
        try:
            import json
            parsed = json.loads(sa_json) if isinstance(sa_json, str) else sa_json
            if parsed.get("type") != "service_account":
                raise ValueError("Not a service account key")
        except Exception as e:
            return ConnectorStatus(connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY, auth_status=AuthStatus.INVALID_CREDENTIALS,
                error=f"Invalid service account JSON: {e}")
        await self.save_config(config)
        return ConnectorStatus(connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.PENDING)

    # NO authorize() — gateway calls authorize_service_account() automatically
```
8. **OAuth2 connectors MUST include `client_id` and `client_secret` in `install_fields`** — these are required for the OAuth flow and must be entered by the user at deploy time, not read from server environment variables
"""

# Sentinel string — detects if the active DB record is missing the latest guidelines section.
# seed_default_guidelines() auto-upgrades to a new version when this sentinel is absent.
# Update this string whenever DEFAULT_CONNECTOR_DEVELOPMENT_MD gains a new section that
# all running instances should adopt without a manual save.
# Updated: SRP-A / SRP-B / OCP-3 rules added — sentinel bumped to "BACKOFF_FACTOR"
_DESIGN_PRINCIPLES_SENTINEL = "BARE module name ONLY (NEVER package-prefixed)"


def _get_r2():
    """Lazy import r2_service to avoid circular imports."""
    from integration.services import r2_service
    return r2_service


async def _get_redis():
    """Lazy Redis connection."""
    import redis.asyncio as aioredis
    return await aioredis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)


# ── Version helpers ───────────────────────────────────────────────────

def _bump_version(current: str) -> str:
    """Increment the patch version: '1.0.0' → '1.0.1'."""
    parts = current.split(".")
    if len(parts) == 3:
        parts[2] = str(int(parts[2]) + 1)
        return ".".join(parts)
    return "1.0.1"


# ── MongoDB helpers ───────────────────────────────────────────────────

def _guidelines_collection():
    from integration.db.database import get_db
    return get_db()["connector_guidelines"]


# ── Public API ────────────────────────────────────────────────────────

async def get_active_guidelines() -> Dict[str, Any]:
    """Return active guidelines dict: {version, content, updated_at}.

    Cache hierarchy: Redis → MongoDB (metadata) + R2 (content) → default.
    """
    try:
        # 1. Get active version from MongoDB
        col = _guidelines_collection()
        doc = await col.find_one({"is_active": True}, sort=[("created_at", -1)])
        if not doc:
            return _default_record()

        version = doc["version"]
        redis_key = _REDIS_KEY_TPL.format(version=version)

        # 2. Try Redis cache
        try:
            r = await _get_redis()
            cached = await r.get(redis_key)
            await r.aclose()
            if cached:
                logger.debug("guidelines.cache_hit", version=version)
                return {"version": version, "content": cached, "updated_at": str(doc.get("created_at", ""))}
        except Exception as exc:
            logger.warning("guidelines.redis_error", error=str(exc))

        # 3. Read from R2 or local cache
        r2 = _get_r2()
        versioned_key = f"{_GUIDELINES_R2_PREFIX}/{_VERSIONED_KEY_TPL.format(version=version)}"
        content = await _r2_get_text(r2, versioned_key)

        if content:
            # Cache in Redis
            try:
                r = await _get_redis()
                await r.setex(redis_key, 3600, content)
                await r.aclose()
            except Exception:
                pass
            return {"version": version, "content": content, "updated_at": str(doc.get("created_at", ""))}

        # 4. Fallback: use content stored in MongoDB doc
        content = doc.get("content", DEFAULT_CONNECTOR_DEVELOPMENT_MD)
        return {"version": version, "content": content, "updated_at": str(doc.get("created_at", ""))}

    except Exception as exc:
        logger.error("guidelines.get_failed", error=str(exc))
        return _default_record()


async def _ingest_guidelines_to_rag(content: str, title: str, guideline_type: str = "code") -> None:
    """Ingest guidelines into MCP RAG so they're semantically searchable.

    Uses a well-known doc_id so re-ingestion replaces the previous version
    instead of duplicating. Every tenant's connectors benefit from this
    global knowledge base.

    guideline_type: "code" for CODE_EXECUTION_GUIDELINES, "docs" for DOCUMENTATION_GUIDELINES
    """
    try:
        from integration.services import knowledge_service
        # Use a deterministic doc_id so updates replace, not duplicate
        doc_id = f"guidelines_{guideline_type}_global"
        kb_id = f"codegen-guidelines-global"
        await knowledge_service._ingest_to_mcp(
            content=content,
            title=title,
            kb_id=kb_id,
            tenant_id="__global__",  # shared across all tenants
            doc_id=doc_id,
        )
        logger.info(f"guidelines.rag_ingested", type=guideline_type, doc_id=doc_id)
    except Exception as exc:
        # Non-fatal — guidelines still work via direct prompt injection
        logger.warning(f"guidelines.rag_ingest_failed", type=guideline_type, error=str(exc))


async def save_guidelines(content: str, change_description: str = "") -> Dict[str, Any]:
    """Save new version of guidelines to MongoDB + R2 + Redis + RAG.

    Deactivates previous active version, creates new one.
    Returns {version, content, updated_at}.
    """
    col = _guidelines_collection()

    # Get current version to compute next
    prev = await col.find_one({"is_active": True}, sort=[("created_at", -1)])
    prev_version = prev["version"] if prev else "1.0.0"
    new_version = _bump_version(prev_version) if prev else "1.0.0"

    now = datetime.now(timezone.utc)

    # Deactivate all current active
    await col.update_many({"is_active": True}, {"$set": {"is_active": False}})

    # Insert new version
    doc = {
        "version": new_version,
        "content": content,
        "change_description": change_description,
        "created_at": now,
        "is_active": True,
    }
    await col.insert_one(doc)

    # Save to R2
    r2 = _get_r2()
    versioned_key = f"{_GUIDELINES_R2_PREFIX}/{_VERSIONED_KEY_TPL.format(version=new_version)}"
    standard_key = f"{_GUIDELINES_R2_PREFIX}/{_STANDARD_KEY}"
    await _r2_put_text(r2, versioned_key, content)
    await _r2_put_text(r2, standard_key, content)  # keep standard up-to-date

    # Cache in Redis
    try:
        r = await _get_redis()
        redis_key = _REDIS_KEY_TPL.format(version=new_version)
        await r.setex(redis_key, 3600, content)
        await r.aclose()
    except Exception as exc:
        logger.warning("guidelines.redis_cache_failed", error=str(exc))

    # Ingest into MCP RAG for semantic search
    await _ingest_guidelines_to_rag(content, f"Code Execution Guidelines v{new_version}", "code")

    logger.info("guidelines.saved", version=new_version)
    return {"version": new_version, "content": content, "updated_at": str(now)}


async def get_version_history() -> List[Dict[str, Any]]:
    """Return all versions from MongoDB, newest first."""
    try:
        col = _guidelines_collection()
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
        logger.error("guidelines.history_failed", error=str(exc))
        return []


async def seed_default_guidelines() -> None:
    """Seed / upgrade the connector_development.md on startup.

    Called from main.py lifespan.

    Behaviour:
    - First boot (no records): creates v1.0.0 in MongoDB + R2/local.
    - Subsequent boots: checks if the active record contains the Design Principles
      sentinel string.  If it is missing (older seed), auto-upgrades to a new version
      so the running instance always has up-to-date guidelines.
    """
    try:
        col = _guidelines_collection()
        active = await col.find_one({"is_active": True}, sort=[("created_at", -1)])

        if active:
            # Check whether the active version already has the Design Principles section
            if _DESIGN_PRINCIPLES_SENTINEL in active.get("content", ""):
                logger.info("guidelines.seed_skipped", reason="already_up_to_date",
                            version=active.get("version"))
                return
            # Upgrade: create a new version with the updated default content
            logger.info("guidelines.seed_upgrading",
                        from_version=active.get("version"),
                        reason="missing Authentication Types section")
            await save_guidelines(
                DEFAULT_CONNECTOR_DEVELOPMENT_MD,
                change_description=(
                    "Auto-upgrade: added SRP-A (connector.py delegates all API calls through client, "
                    "never calls SDK directly), SRP-B (helpers return final encoded form, connector.py "
                    "never does base64/MIME inline), OCP-3 (retry delays as class constants "
                    "INITIAL_BACKOFF_S/BACKOFF_FACTOR, never hardcoded literals)."
                ),
            )
            return

        # First boot — no records at all
        now = datetime.now(timezone.utc)
        doc = {
            "version": "1.0.0",
            "content": DEFAULT_CONNECTOR_DEVELOPMENT_MD,
            "change_description": "Initial default — Shielva standard with Design Principles",
            "created_at": now,
            "is_active": True,
        }
        await col.insert_one(doc)
        logger.info("guidelines.seed_mongodb", version="1.0.0")

        # Write to R2 / local storage
        r2 = _get_r2()
        standard_key = f"{_GUIDELINES_R2_PREFIX}/{_STANDARD_KEY}"
        versioned_key = f"{_GUIDELINES_R2_PREFIX}/{_VERSIONED_KEY_TPL.format(version='1.0.0')}"
        await _r2_put_text(r2, standard_key, DEFAULT_CONNECTOR_DEVELOPMENT_MD)
        await _r2_put_text(r2, versioned_key, DEFAULT_CONNECTOR_DEVELOPMENT_MD)
        logger.info("guidelines.seed_r2", standard_key=standard_key, versioned_key=versioned_key)

        # Ingest into MCP RAG on first boot
        await _ingest_guidelines_to_rag(
            DEFAULT_CONNECTOR_DEVELOPMENT_MD,
            "Code Execution Guidelines v1.0.0",
            "code",
        )

    except Exception as exc:
        logger.warning("guidelines.seed_failed", error=str(exc))


# ── Internal helpers ─────────────────────────────────────────────────

def _default_record() -> Dict[str, Any]:
    return {
        "version": "1.0.0",
        "content": DEFAULT_CONNECTOR_DEVELOPMENT_MD,
        "updated_at": "",
    }


async def _r2_get_text(r2, key: str) -> Optional[str]:
    """Read text from R2 or local cache. Returns None if not found."""
    try:
        if r2._use_local():
            local_path = Path(r2._LOCAL_CACHE_DIR) / key
            if local_path.exists():
                return local_path.read_text(encoding="utf-8")
            return None
        loop = asyncio.get_event_loop()
        import boto3
        import botocore.exceptions
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
        get_fn = partial(s3.get_object, Bucket=settings.R2_BUCKET_NAME, Key=key)
        resp = await loop.run_in_executor(None, get_fn)
        return resp["Body"].read().decode("utf-8")
    except Exception:
        return None


async def _r2_put_text(r2, key: str, content: str) -> None:
    """Write text to R2 or local cache."""
    try:
        if r2._use_local():
            local_path = Path(r2._LOCAL_CACHE_DIR) / key
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(content, encoding="utf-8")
            return
        loop = asyncio.get_event_loop()
        import boto3
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
        put_fn = partial(
            s3.put_object,
            Bucket=settings.R2_BUCKET_NAME,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown",
        )
        await loop.run_in_executor(None, put_fn)
    except Exception as exc:
        logger.warning("guidelines.r2_put_failed", key=key, error=str(exc))
