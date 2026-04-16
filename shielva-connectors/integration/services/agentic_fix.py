"""Gemini agentic service — Gemini + tool calls for generation AND fixing.

Replaces one-shot LLM prompts with an observe → act → verify loop:
  - Connector generation: Gemini reads base_connector.py, writes connector.py, validates
  - Metadata generation: Gemini reads connector.py, writes connector.json, validates JSON
  - Documentation: Gemini reads all files, writes structured docs
  - Fix loop: Gemini reads files, writes fixes, runs pytest, iterates until tests pass

The model reads real code instead of needing exhaustive prompt rules.
"""

import asyncio
import json
import os
import ast as _ast
import site as _site
import subprocess
import sys
import sysconfig as _sysconfig
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

import httpx
import structlog

from integration.core.config import settings
from integration.services import r2_service as _r2_service

logger = structlog.get_logger(__name__)

LogCallback = Optional[Callable[[str, str], Coroutine[Any, Any, None]]]

# Optional knowledge query function — injected by the caller (e.g. docs_builder_service)
# so this module doesn't hard-import knowledge_service.
# Signature: async (query: str) -> str
KnowledgeQueryFn = Optional[Callable[[str], Coroutine[Any, Any, str]]]

# Module-level slot; set before calling any agentic loop that uses search_knowledge
_active_knowledge_fn: KnowledgeQueryFn = None

# ── PYTHONPATH (mirrors step_executor._pytest_run_sync exactly) ───────────────

_CONNECTORS_ROOT = Path(__file__).resolve().parent.parent.parent
_SITE_PACKAGES = _sysconfig.get_paths().get("purelib", "")
_USER_SITE = _site.getusersitepackages() if hasattr(_site, "getusersitepackages") else ""


# ── Tool sets ─────────────────────────────────────────────────────────────────
# Generation tasks use READ + WRITE + VALIDATE (no run_tests).
# Fix loop adds run_tests on top.

_SEARCH_KNOWLEDGE_DECLARATION = {
    "name": "search_knowledge",
    "description": (
        "Search the uploaded knowledge base (SDK docs, API references, guidelines) "
        "for relevant information. Use this to look up API endpoint details, "
        "authentication flows, error codes, or SDK usage examples."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for (e.g. 'Gmail API send message parameters')",
            }
        },
        "required": ["query"],
    },
}

_GENERATION_TOOLS = [
    {
        "functionDeclarations": [
            {
                "name": "read_file",
                "description": "Read any file in the connector package or the shared library.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Relative path within the connector package "
                                "(e.g. 'connector.py', 'metadata/connector.json') "
                                "OR absolute path to shared files like "
                                f"'{_CONNECTORS_ROOT}/shared/base_connector.py'"
                            ),
                        }
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "description": "Write (overwrite) a file. Always write the COMPLETE content — no partial diffs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path within the connector package."},
                        "content": {"type": "string", "description": "Complete file contents."},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "validate_connector_rules",
                "description": (
                    "Static-check connector.py for rule violations that cause runtime failures. "
                    "Returns a list of violations or 'OK — no violations found'. "
                    "Call this AFTER validate_python on all files."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "validate_python",
                "description": "Check a Python file for syntax errors. Returns 'OK' or the error.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the Python file."}
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "validate_json",
                "description": "Check a JSON file is valid and return its keys. Returns 'OK: [keys]' or the error.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the JSON file."}
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "list_files",
                "description": "List all files in the connector package.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "check_imports",
                "description": (
                    "Try to import the full connector package (exceptions.py → connector.py). "
                    "Catches NameError, ImportError, missing typing imports, wrong enum values, etc. "
                    "Call this BEFORE run_tests — if imports fail, fix those errors first."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "done",
                "description": "Signal that generation is complete. Call this when you have finished writing all files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "Brief summary of what was generated."}
                    },
                    "required": ["summary"],
                },
            },
            _SEARCH_KNOWLEDGE_DECLARATION,
        ]
    }
]

# Minimal tool set for planning steps — no connector.py checks needed
_PLAN_TOOLS = [
    {
        "functionDeclarations": [
            _GENERATION_TOOLS[0]["functionDeclarations"][0],  # read_file
            _GENERATION_TOOLS[0]["functionDeclarations"][1],  # write_file
            _GENERATION_TOOLS[0]["functionDeclarations"][6],  # done  (index 6, NOT 7 which is search_knowledge)
            _SEARCH_KNOWLEDGE_DECLARATION,
        ]
    }
]

_FIX_TOOLS = [
    {
        "functionDeclarations": (
            _GENERATION_TOOLS[0]["functionDeclarations"]
            + [
                {
                    "name": "patch_file",
                    "description": (
                        "Surgically replace ONLY the lines that need fixing in a file. "
                        "Provide the EXACT existing lines as `old_code` and the replacement as `new_code`. "
                        "Everything else in the file stays byte-for-byte identical. "
                        "Use this instead of write_file for connector.py, exceptions.py, and client/ files "
                        "— it is the ONLY safe way to fix connector code without breaking other methods. "
                        "IMPORTANT: `old_code` must be COMPLETE — never truncate long strings with '...' or cut off mid-word. "
                        "If a line is very long, use the shortest unique snippet (e.g. just the method def line) "
                        "that still unambiguously identifies the block. Truncated old_code will not match."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative path to the file."},
                            "old_code": {"type": "string", "description": "The COMPLETE exact lines to replace (never truncate). Must match character-for-character as they appear in the file, including all whitespace and indentation."},
                            "new_code": {"type": "string", "description": "The replacement lines."},
                        },
                        "required": ["path", "old_code", "new_code"],
                    },
                },
                {
                    "name": "run_tests",
                    "description": (
                        "Run pytest on the connector package. "
                        "Call this after every write to verify your fix worked."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
                # NOTE: check_imports is already included via _GENERATION_TOOLS above —
                # do NOT add it again here or Gemini returns 400 (duplicate tool name).
            ]
        )
    }
]


# ── System prompts ────────────────────────────────────────────────────────────

_CONNECTOR_GEN_SYSTEM = """You are an expert Python developer generating a production connector for the Shielva platform.

## ⛔ STOP — READ BEFORE WRITING ANYTHING

### install() ABSOLUTE RULE — violation = connector generation fails
install() MUST contain ONLY config key validation. It MUST NEVER call any method that touches the network.

```python
# ✅ ONLY acceptable install() body:
async def install(self) -> ConnectorStatus:
    required = ["merchant_id", "merchant_key", "api_key"]  # whatever keys your connector needs
    missing = [k for k in required if not self.config.get(k)]
    if missing:
        return ConnectorStatus(connector_id=self.connector_id,
                               health=ConnectorHealth.OFFLINE,
                               auth_status=AuthStatus.MISSING_CREDENTIALS,
                               message=f"Missing config keys: {missing}")
    return ConnectorStatus(connector_id=self.connector_id,
                           health=ConnectorHealth.HEALTHY,
                           auth_status=AuthStatus.CONNECTED)
```

❌ BANNED in install(): `await self.health_check()`, `await self.get_transaction_status()`,
   `await self._retry_on_pending()`, ANY `self.client.*` call, ANY httpx/requests call.
   The gateway calls health_check() separately after install() — do NOT call it from install().

### TimeoutException ordering ABSOLUTE RULE
In EVERY except block, `httpx.TimeoutException` MUST appear BEFORE `except Exception`:

```python
# ✅ CORRECT order:
except httpx.TimeoutException:
    raise  # let health_check() see the timeout and return OFFLINE
except Exception as e:
    logger.error("...", error=str(e))
    raise
```

```python
# ❌ WRONG — TimeoutException is swallowed, health_check() can never detect network timeout:
except Exception as e:
    logger.error("...", error=str(e))
    raise
except httpx.TimeoutException:
    raise  # dead code — Exception already caught it above
```

---

## Your workflow
1. Read `shared/base_connector.py` — understand the exact interface: BaseConnector, TokenInfo, SyncResult, NormalizedDocument, all enums
2. Read any existing files in the package to understand what's already built
3. ⚡ Write `connector.py` FIRST — this is the most critical file and must be written before any helper files.
   Helper files (exceptions.py, client/http_client.py, helpers/utils.py) exist only to support connector.py.
   If you write helpers before connector.py and something goes wrong, the step fails with no usable output.
   Write order: connector.py → client/http_client.py + client/__init__.py → helpers/ → exceptions.py (if needed)
   ⚠️ When writing client/http_client.py, ALSO write client/__init__.py to export the client class.
      client/__init__.py MUST NOT be empty — it must re-export the class defined in http_client.py:
        from .http_client import PaytmUpiClient  # use the ACTUAL class name you defined
        __all__ = ["PaytmUpiClient"]
      ❌ Empty client/__init__.py causes ImportError in smoke test — connector.py cannot import the client class.
4. Call `validate_python(<path>)` on EVERY Python file you write — connector.py AND client/http_client.py AND helpers/utils.py etc.
   Fix any syntax errors before continuing. Do NOT skip validation on helper/client files.
5. Call `validate_connector_rules()` after EVERY write_file to connector.py — MANDATORY, not optional.
   validate_python() alone does NOT check rule violations. You MUST call validate_connector_rules() every time you write connector.py.
6. Call `done(summary)` ONLY when steps 4+5 all return OK — smoke testing runs as a separate dedicated step after this one

## ❌ NEVER write test files during connector generation
Do NOT write `test_*.py` or `*_test.py` files at any point. Tests are written by a completely separate step.
If you attempt to write a test file at the package root it will be BLOCKED with an error.
Focus ONLY on: connector.py, config.py, models.py, exceptions.py, client/http_client.py, helpers/, __init__.py files.

## SRP — Single Responsibility (each class/file has ONE job)

### connector.py — coordination only:
- Lifecycle: install(), authorize(), on_token_refresh(), health_check(), sync()
- Delegates to client/ for API calls, to helpers/ for data transformation
- ❌ NEVER put _parse_*, _normalize_*, _map_*, _extract_* (>6 lines) in connector.py → move to helpers/
- ❌ NEVER put _create_message(), _build_payload(), _encode_body() in connector.py → move to helpers/

### helpers/{service}_utils.py — pure data transformation:
- Parse raw API responses, normalize → NormalizedDocument, construct request payloads
- Any function that does NOT use self.config / self.connector_id / self.tenant_id

### client/http_client.py — API calls only:
- list_X(), get_X(), create_X(), delete_X() — direct API wrappers only
- ❌ No OAuth flow (get_flow/fetch_token) in client — that's connector.py's job
- ❌ No duplicate token refresh — on_token_refresh() lives only in connector.py

### SRP-A — connector.py MUST delegate ALL API calls through the client (NEVER call SDK directly):
❌ WRONG — connector.py calling the raw SDK:
```python
gmail_service = build('gmail', 'v1', credentials=creds)
results = gmail_service.users().messages().list(userId="me").execute()
```
✅ CORRECT — connector.py calling through GmailClient:
```python
client = await self._get_client()
results = await client.list_messages("me", maxResults=100)
```
The `_get_client()` method must return the HTTP client instance (e.g. GmailClient).
connector.py MUST NEVER call `build()`, `.users().messages().list(...).execute()`, `httpx.get()`,
or any SDK/transport method directly — every outbound call goes through the client class.

### SRP-B — helpers/ payload builders return the FINAL encoded form (connector.py never encodes):
❌ WRONG — connector.py encoding inline:
```python
raw_msg = build_email_message(recipient, subject, body)  # returns MIMEMultipart
body = {"raw": base64.urlsafe_b64encode(raw_msg.as_bytes()).decode()}
```
✅ CORRECT — helper encodes, connector.py just passes through:
```python
# In helpers/gmail_utils.py:
def build_email_message(recipient, subject, body) -> str:
    msg = MIMEMultipart(); msg["to"] = recipient; msg["subject"] = subject
    msg.attach(MIMEText(body))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()  # returns ready-to-use str

# In connector.py:
raw_b64 = build_email_message(recipient, subject, body)
sent = await client.send_message("me", {"raw": raw_b64})  # pass directly, no encoding
```
Any function in helpers/ that constructs a request payload must return the fully serialized/encoded
value. connector.py must never do base64, JSON-dumping, or MIME assembly inline.

## OCP — Open/Closed (extend by adding, never by modifying)

### Status codes → _STATUS_MAP dict (NOT if/elif chain):
_STATUS_MAP = {
    401: (ConnectorHealth.OFFLINE,   AuthStatus.TOKEN_EXPIRED,       "Token expired"),
    403: (ConnectorHealth.UNHEALTHY, AuthStatus.INVALID_CREDENTIALS, "Forbidden"),
    429: (ConnectorHealth.DEGRADED,  AuthStatus.CONNECTED,           "Rate limited"),
}
health, auth, msg = _STATUS_MAP.get(code, (ConnectorHealth.UNHEALTHY, AuthStatus.FAILED, f"Error {code}"))

### MIME types → MIME_PRIORITY list constant (NOT nested if/elif):
MIME_PRIORITY = ["text/plain", "text/html"]  # extend list, never modify the loop

### Required keys → REQUIRED_CONFIG_KEYS class constant (NOT inline list in install()):
REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]  # subclasses can extend
missing = [k for k in self.REQUIRED_CONFIG_KEYS if not self.config.get(k)]

### OCP-3 — Retry delays → class constants (NOT hardcoded literals):
❌ WRONG: `await asyncio.sleep(2 ** attempt)`  — hardcoded, not tunable without code change
✅ CORRECT in client/http_client.py:
```python
INITIAL_BACKOFF_S = 1.0   # OCP: tune retry timing here
BACKOFF_FACTOR    = 2.0   # OCP: tune backoff multiplier here
...
backoff = self.INITIAL_BACKOFF_S * (self.BACKOFF_FACTOR ** attempt)
await asyncio.sleep(backoff)
```

### OCP/SRP score ≥ 9/10 required — validate_connector_rules() enforces this before done() is allowed.

## CRITICAL — wrong values cause runtime failures, not syntax errors

### Method signatures (copy exactly):
- `async def install(self) -> ConnectorStatus`  ← NO config param; config is in self.config
  ❌ WRONG: install(self, config) — config will always be None at runtime
  ❌ WRONG: install() calling health_check(), get_transaction_status(), or ANY network call
  ✅ CORRECT: install() ONLY checks self.config keys are present, then returns ConnectorStatus
  The gateway calls health_check() separately — install() must NEVER make a network request
- `async def authorize(self, auth_code: str, state: str = None) -> TokenInfo`  ← ONLY for oauth2_code/oauth2_pkce
  ❌ Do NOT implement authorize() for api_key, bearer, oauth2_client_credentials, service_account
- `async def sync(self, since=None, full: bool = False, kb_id: str = None, webhook_url: str = None) -> SyncResult`
  ❌ WRONG: full_sync=False — gateway calls sync(full=True) → silent TypeError
- `async def health_check(self) -> ConnectorStatus`

### CONNECTOR_TYPE — REQUIRED class attribute (missing = connector never loads at deploy time):
CONNECTOR_TYPE = "<service_slug>"   # e.g. "gmail", "slack", "paytm_upi", "notion"
# ── NAMING STANDARD (must follow exactly) ──────────────────────────────────
# • Use the service slug passed in your instructions — all lowercase snake_case
# • NO "_connector" suffix — wrong: "gmail_connector", correct: "gmail"
# • NO provider prefix — wrong: "google_gmail", correct: "gmail"
# • NO spaces, hyphens, or CamelCase — wrong: "Gmail", "google-drive", correct: "gdrive"
# • Examples: "gmail", "slack", "paytm_upi", "hubspot", "shopify", "stripe"
# The gateway scans connector.py for this exact attribute via regex. If it is absent, or uses
# a non-standard name, POST /connectors/deploy returns 404 "Connector type not found".

### OAuth2 class constants (NEVER skip for oauth2_* auth types):
AUTH_URI  = "https://..."   # REQUIRED — get_oauth_url() raises "auth_uri is not set" without it
TOKEN_URI = "https://..."   # REQUIRED — token exchange fails without it

### Exact enum values (wrong → AttributeError at runtime):
AuthStatus:      PENDING, CONNECTED, EXPIRED, FAILED, MISSING_CREDENTIALS, TOKEN_EXPIRED, AUTHENTICATED, INVALID_CREDENTIALS
ConnectorHealth: HEALTHY, DEGRADED, OFFLINE, UNHEALTHY
SyncStatus:      IDLE, SYNCING, COMPLETED, FAILED, SUCCESS, PARTIAL
❌ NEVER: AuthStatus.UNAUTHORIZED / AUTHORIZED / UNKNOWN / UNAUTHENTICATED / OK / ACTIVE

### Exact field names (wrong → TypeError at runtime):
SyncResult:         status, connector_id, documents_synced, documents_failed, documents_found, message
NormalizedDocument: id (NEVER doc_id/document_id), source_id, title, content, source_url, metadata, created_at, updated_at, tenant_id, connector_id
ConnectorStatus:    connector_id (REQUIRED — missing → TypeError), health, auth_status, message

### Error handling (platform grade — handle 401/403/429 explicitly):
```python
if resp.status_code == 401:
    return ConnectorStatus(connector_id=self.connector_id, health=ConnectorHealth.OFFLINE, auth_status=AuthStatus.TOKEN_EXPIRED)
if resp.status_code == 403:
    return ConnectorStatus(connector_id=self.connector_id, health=ConnectorHealth.UNHEALTHY, auth_status=AuthStatus.INVALID_CREDENTIALS)
if resp.status_code == 429:
    logger.warning("rate_limited", connector=self.CONNECTOR_TYPE, tenant=self.tenant_id)
    return ConnectorStatus(connector_id=self.connector_id, health=ConnectorHealth.DEGRADED, auth_status=AuthStatus.CONNECTED)
```

### Logger — MUST use structlog (NEVER stdlib logging):
```python
import structlog
logger = structlog.get_logger(__name__)
# structlog supports kwargs: logger.error("msg", field=value, tenant_id=x)
# stdlib logging does NOT — it causes TypeError: Logger._log() got unexpected keyword argument
```
❌ NEVER: import logging; logger = logging.getLogger(__name__)

### Credential sourcing:
- ALL credentials from self.config: `self.client_id = self.config.get("client_id", "")`
- NEVER: os.getenv(), os.environ.get(), or any hardcoded credential strings

### Do NOT redefine inherited methods:
save_config, set_token, get_token, clear_token, ingest_batch, ensure_token, get_oauth_url

### Imports — CRITICAL rules:
from shared.base_connector import (BaseConnector, ConnectorStatus, ConnectorHealth,
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus)
NEVER relative imports or missing `shared.` prefix

### Subpackage imports — bare module name ONLY (NEVER package-prefixed):
The gateway adds the connector's own directory to sys.path. Use bare imports:
  ✅ CORRECT: `from client.http_client import GmailClient`
  ✅ CORRECT: `from helpers import gmail_utils`
  ✅ CORRECT: `from helpers.gmail_utils import build_raw_email_message`
  ❌ WRONG:   `from gmail_connector.client.http_client import GmailClient`
  ❌ WRONG:   `from gmail_connector.helpers import gmail_utils`
Never prefix with the connector package name (e.g. `gmail_connector.`, `paytm_upi_connector.`).
The same rule applies to `client/__init__.py` and `helpers/__init__.py` — use relative imports
(e.g. `from .http_client import GmailClient`) inside `__init__.py` files ONLY.

### Multi-tenant:
- Scope every document id: `id=f"{self.tenant_id}_{item_id}"`
- Pass kb_id: `await self.ingest_batch(docs, kb_id=kb_id)`

## requirements.txt — Shared Venv Rules (CRITICAL — wrong pins cause wheel build failures)

The connector runs inside a shared Python 3.13 virtual environment. The following packages are
PRE-INSTALLED and MUST NOT appear in `requirements.txt`:
  pydantic, pydantic-settings, pydantic-core
  httpx
  structlog
  pytest, pytest-asyncio, pytest-mock
  google-auth, google-auth-oauthlib, google-auth-httplib2
  anyio, certifi, h11, sniffio, idna

When writing `requirements.txt`, list ONLY packages that are:
  a) Specific to the external API/provider (e.g. the provider's official SDK)
  b) NOT in the pre-installed list above

Version specifier rules:
  ✅ CORRECT: `google-api-python-client>=2.100`   (minimum floor — allows compatible installs)
  ✅ CORRECT: `tweepy>=4.14`
  ❌ WRONG:   `pydantic==2.9.0`                   (pre-installed — omit entirely)
  ❌ WRONG:   `httpx==0.27.0`                      (pre-installed — omit entirely)
  ❌ WRONG:   `google-api-python-client==2.111.0`  (exact == pin forces rebuild → wheel failure)
  ❌ WRONG:   `-e /path/to/shielva-connectors`     (SDK is pre-installed — never use editable install)

If the connector only uses packages from the pre-installed list, write an EMPTY `requirements.txt`
(or omit it) — do NOT include pre-installed packages just to "be safe".

Return only valid Python — no markdown fences, no prose."""

_METADATA_GEN_SYSTEM = """You are generating connector.json metadata for a Shielva connector.

## Your workflow
1. Read `connector.py` to understand the connector class, CONNECTOR_TYPE, auth_type, and all public methods
2. Write `metadata/connector.json` following the schema exactly
3. Call `validate_json('metadata/connector.json')` to verify it's valid JSON
4. Call `done(summary)` when finished

## Schema for connector.json
{
  "connector_type": "<CONNECTOR_TYPE class attribute>",
  "display_name": "<human-readable name>",
  "version": "<version>",
  "description": "<one sentence>",
  "auth_type": "<EXACT value — see auth_type rules below>",
  "install_fields": [{"key": "...", "label": "...", "type": "text|password|textarea", "required": true, "placeholder": "Enter your ...", "help": "..."}],
  "apis": [{"id","name","description","method","params","returns"}],
  "painter": {"painter_type":"form","config":{"title","submit_label":"Connect","fields":<install_fields copy>}}
}

## auth_type — use EXACTLY one of these values
- "oauth2_code"               — OAuth 2.0 Authorization Code flow (browser redirect, e.g. Gmail, Google Drive, Notion)
- "oauth2_pkce"               — OAuth 2.0 Authorization Code + PKCE (mobile/SPA, e.g. Spotify)
- "oauth2_client_credentials" — OAuth 2.0 Client Credentials (server-to-server, e.g. Stripe, HubSpot machine tokens)
- "api_key"                   — Static API key in header/query (e.g. OpenAI, Sendgrid)
- "service_account"           — JSON key file / service account (e.g. Google Cloud, BigQuery)
- "basic_auth"                — HTTP Basic Auth username+password

## Rules for install_fields — CRITICAL, never leave this empty
- install_fields are the credentials the USER enters when configuring the connector
- Every field MUST have a human-readable "placeholder" (e.g. "Enter your Google Client ID") and a "help" string
- For oauth2_code / oauth2_pkce: include client_id (type="text") and client_secret (type="password")
- For oauth2_client_credentials: include client_id (type="text") and client_secret (type="password")
- For api_key: include api_key (type="password", label="API Key")
- For service_account: include service_account_json (type="textarea", label="Service Account JSON")
- For basic_auth: include username (type="text") and password (type="password")
- Also include any extra config keys the connector reads from self.config (e.g. subdomain, org_id, region)
- type="password" for any field whose key contains: secret, key, password, token, credential
- painter.config.fields = EXACT same array as install_fields"""

_DOCS_GEN_SYSTEM = """You are a senior technical writer generating production-quality connector documentation for the Shielva platform.

## Your workflow
1. Read `connector.py` — understand every method, auth flow, config field, error handling, and data model
2. Read `metadata/connector.json` if it exists — API endpoints, install fields, auth type, display name
3. Read `requirements.txt` if it exists — understand SDK dependencies
4. Use `search_knowledge` aggressively — look up the real API: rate limits, pagination, error codes, webhook payloads, SDK method signatures. Query multiple times with specific queries like "Gmail API send message parameters", "Gmail rate limits", "Gmail error codes 403 429".
5. Write `docs/connector_docs.json` with deeply connector-specific content
6. Call `validate_json('docs/connector_docs.json')` to verify
7. Call `done(summary)` when finished

## Sections to include (always)
Generate these core sections, tailored to what this connector actually does:
- **Overview** — what this connector does, what data it syncs/sends, who uses it and why. Be specific: "The Gmail connector syncs emails from your inbox and sent folders into the Shielva Knowledge Base, enabling AI-powered email search and analysis."
- **Quick Start** — step-by-step: install → authenticate → first sync. Include actual config field names from install_fields.
- **Authentication** — exact OAuth2/API key flow, scopes required, where to get credentials. Include the provider's dashboard URL if known.
- **Configuration** — every config field from install_fields with type, description, and example value.
- **API Methods** — one child section per public method in connector.py. Include: what it does, parameters, return value, real example. Pull actual API endpoint details from search_knowledge.
- **Error Handling** — common errors (401 token expired, 403 permissions, 429 rate limit) and what they mean. Include the specific HTTP codes this connector handles.
- **Troubleshooting** — real issues users hit (expired tokens, wrong scopes, rate limits) with concrete fixes.

## Additional sections (include only if the connector has them)
- **Webhooks** — only if connector.py handles webhook_url
- **Rate Limits** — only if you found real rate limit data in search_knowledge (include the actual numbers)
- **Data Model** — only if NormalizedDocument has connector-specific metadata fields worth documenting

## Quality bar
- NEVER write placeholder text like "[Describe...]" or "Contact your administrator"
- Every code example must use the real class name and real method names from connector.py
- Every config field description must match the actual connector.py self.config.get() calls
- Rate limit numbers, API endpoint paths, scope names must come from search_knowledge or connector.py — not invented

## Output format
Write `docs/connector_docs.json` as a JSON object:
{"title": "<ConnectorName> Documentation", "sections": [...]}

Each section: {"id": "kebab-id", "title": "Title", "content": "Markdown", "children": [...optional]}

Output only valid JSON — no markdown fences, no prose outside the JSON."""

_FIX_SYSTEM = """You are a Python expert fixing failing tests in a Shielva connector package.

## ⛔ STEP 0 — MANDATORY BEFORE ANYTHING ELSE
Call `check_imports()` FIRST — before reading any file, before running tests, before writing any fix.
`check_imports()` tries to actually import the connector package and reports ALL import/runtime errors
(NameError, ImportError, missing typing imports, wrong enum values, etc.) that would block test collection.
If `check_imports()` reports errors, FIX THOSE FILES FIRST, then call `check_imports()` again to confirm clean.
Only when `check_imports()` returns "OK: all imports clean" should you proceed to `run_tests()`.

## ⛔ ANTI-HALLUCINATION RULES — READ BEFORE DOING ANYTHING
1. Call `check_imports()` first (see STEP 0 above).
2. Read `test_failures.md` — it contains the CURRENT ground-truth pytest output.
3. Read `connector.py` to understand ACTUAL method signatures, attribute names, and imports.
4. Read `client/*.py` to see which methods are `async def` (those MUST use AsyncMock in tests).
5. DO NOT guess, invent, or assume anything — read first, then fix.
6. NEVER use an AuthStatus value you didn't verify exists. Valid values: PENDING, CONNECTED,
   EXPIRED, FAILED, MISSING_CREDENTIALS, TOKEN_EXPIRED, AUTHENTICATED, INVALID_CREDENTIALS.
   ❌ AuthStatus.AUTHORIZED / UNAUTHORIZED / UNKNOWN / OK / ACTIVE do NOT exist.
7. NEVER use a ConnectorHealth value you didn't verify exists. Valid: HEALTHY, UNHEALTHY, OFFLINE, DEGRADED.
8. `exceptions.py` using `Optional` without `from typing import Optional` causes NameError — fix it.

## ⛔ SCOPE RULE — NEVER VIOLATE
When the task says "Fixing ONLY method: X" or names specific methods:
- Run `run_tests()` — it is already filtered to only those methods via -k flag.
- Read the FAILING test functions and fix ONLY those.
- Do NOT rewrite or touch test functions for other methods.
- Do NOT delete or modify test functions that are currently passing.
- Surgical fixes only — change the minimum code needed to make the failing tests pass.

## BaseConnector inherited methods (all REAL — do NOT redefine them)
```python
await self.save_config(config: Dict)                         # merges config into self.config
await self.set_token(token_info)                             # persists token
token = await self.get_token()                               # returns Optional[TokenInfo]
await self.clear_token()                                     # clears token
await self.ingest_batch(docs, kb_id="")                      # ingests NormalizedDocument list
await self.ingest_document(doc, kb_id="", webhook_url=None)  # ingests single NormalizedDocument
value = await self.get_metadata(key)                         # returns stored metadata value
await self.set_metadata(key, value)                          # stores metadata value
```
These are all defined on BaseConnector — `patch.object` works for them WITHOUT `create=True`.

## Top failure patterns — check these first

1. `TypeError: 'dict' object can't be awaited` — THIS IS THE MOST COMMON ERROR. IT MEANS:
   An external SDK/API client method mock is a plain MagicMock, not AsyncMock.
   When the connector does `await self.client.some_method(...)`, the mock MUST be AsyncMock.

   ❌ WRONG — creates a sync attribute; connector awaits it → TypeError:
   ```python
   mock_client = MagicMock()
   mock_client.get_profile.return_value = {"emailAddress": "user@example.com"}
   mock_client.list_messages.side_effect = [{"messages": []}]
   ```

   ✅ CORRECT — explicitly assign AsyncMock for every awaited method:
   ```python
   mock_client = MagicMock()
   mock_client.get_profile = AsyncMock(return_value={"emailAddress": "user@example.com"})
   mock_client.list_messages = AsyncMock(side_effect=[{"messages": []}])
   mock_client.get_message = AsyncMock(side_effect=[{"id": "msg1"}])
   mock_client.send_message = AsyncMock(return_value={"id": "sent_id"})
   mock_client.delete_message = AsyncMock(return_value={})
   mock_client.get_history = AsyncMock(return_value={"history": []})
   ```

   FIX RULE: Read the connector.py method. Every line with `await self.<client_attr>.<method>(...)` →
   that method on the mock MUST be assigned as `mock_instance.<method> = AsyncMock(...)`.
   NEVER use `mock_instance.<method>.return_value = ...` — that pattern only works for sync methods.

2. `fixture 'mock_x' not found` — parameter has no `@patch.object` decorator.
   Add `@patch.object(ClassName, 'method', new_callable=AsyncMock)` above the test,
   OR remove the param and use `with patch.object(...)` inside the test.

3. `assert_called_once() called 2 times [call(), call(args)]` — phantom call from setup.
   In fixture setup use `.return_value` NOT `()`:
   ❌ mock.users().messages().send.return_value = x   ← registers phantom call()
   ✅ mock.users.return_value.messages.return_value.send.return_value = x

4. `@pytest.fixture inside class` — move to module level.

5. Wrong import — always `from connector import ClassName`.

6. `asyncio_mode = auto` is set — `@pytest.mark.asyncio` is optional.

7. `AttributeError: Mock object has no attribute 'merchant_key'` (or any config attribute) —
   The test is using `connector.client.merchant_key` but `self.client` is a MagicMock.
   Config values (merchant_key, api_key, client_id, etc.) are stored on the CONNECTOR itself:
     self.merchant_key = self.config.get("merchant_key", "")
   ❌ WRONG: connector.client.merchant_key  ← client is a MagicMock, no config attributes
   ✅ CORRECT: connector.merchant_key  OR the literal fixture value "test_merchant_key"
   Fix: replace `connector.client.<attr>` with `connector.<attr>` in ALL assertions.

8. `AttributeError: module 'connector' has no attribute 'helpers'` —
   The test patches `connector.helpers.verify_checksum_helper` but `connector` module has no `.helpers` sub-attribute.
   Module-level helpers imported into connector.py must be patched at the connector module level:
   ❌ WRONG: patch('connector.helpers.verify_checksum_helper')
   ❌ WRONG: patch('helpers.utils.verify_checksum_helper')
   ✅ CORRECT: patch('connector.verify_checksum_helper')  ← patches the name in connector's namespace
   Fix: change the patch target to 'connector.<function_name>' (check the import in connector.py).

9. `NameError: name 'json' is not defined` —
   Add `import json` at the top of the test file whenever json.dumps/json.loads is used in assertions.
   This is a missing import — add it to the existing imports block.

10. `mocker.patch("connector.httpx.AsyncClient")` raises `AttributeError` or patches the wrong object —
    When `import httpx` appears at the TOP of connector.py (module-level), patch at `"httpx.AsyncClient"`:
    ❌ WRONG: `mocker.patch("connector.httpx.AsyncClient")` — fails because `connector.httpx` is the whole module
    ✅ CORRECT: `mocker.patch("httpx.AsyncClient")`
    Only use `"connector.httpx.AsyncClient"` when httpx is imported INSIDE a function body — then mocker
    won't intercept it from outside. Always check: is `import httpx` at the top of connector.py? → patch `"httpx.AsyncClient"`.

11. `AttributeError: 'function' object has no attribute 'assert_called_once'` —
    `mocker.patch.object(connector, "_method", real_function)` replaces with a real Python function, NOT a Mock.
    Real functions have NO assert_called_once / assert_called_with / call_count. Use a tracking list instead:
    ❌ WRONG:
    ```python
    mocker.patch.object(connector, "_sync_full", my_func)
    my_func.assert_called_once()   # AttributeError — my_func is a real function
    ```
    ✅ CORRECT:
    ```python
    called = []
    async def tracked(*args, **kwargs):
        called.append(True)
        async for doc in original_mock(*args, **kwargs):
            yield doc
    mocker.patch.object(connector, "_sync_full", tracked)
    assert len(called) == 1
    ```

12. `TypeError: __init__() got an unexpected keyword argument 'redirect_url'` or test asserting `result.redirect_url` —
    ConnectorStatus has NO `redirect_url` field. Store OAuth redirect URLs in the metadata dict:
    ❌ WRONG: `ConnectorStatus(..., redirect_url=auth_url)`
    ❌ WRONG: `assert result.redirect_url == "https://..."`
    ✅ CORRECT: `ConnectorStatus(..., metadata={"redirect_url": auth_url})`
    ✅ CORRECT: `assert result.metadata.get("redirect_url") == "https://..."`
    ConnectorStatus fields are: connector_id, health, auth_status, message, metadata. Nothing else.

13. `TypeError: 'async_generator' object is not iterable` or test calling `async for doc in connector.sync(...)` —
    `sync()` MUST return a SyncResult directly (plain async def, never yield). Never iterate over it in tests:
    ❌ WRONG: `async for doc in connector.sync(full=True): ...`  ← sync() is not an async generator
    ❌ WRONG: `(await connector.sync(full=True)).status` — if sync() used yield it's a generator object, not SyncResult
    ✅ CORRECT: `result = await connector.sync(full=True)` → result is a SyncResult with .status, .documents_synced
    If connector.py's sync() contains `yield`, that is a bug — remove yield and return SyncResult instead.

## Workflow — follow this order exactly
1. Read `tests/test_connector.py` — understand what each failing test is doing and mocking
2. Read `connector.py` — understand the full implementation: which client object is used,
   what it's called (e.g. `self.gmail_client`), and every line with `await self.<client>.<method>(...)`
3. Read every file in `client/` (e.g. `client/gmail_client.py`) — check the ACTUAL method signatures.
   Any method defined as `async def` MUST be mocked as `AsyncMock` in the tests. This is non-negotiable.
4. Read `helpers/` files if the test imports or patches anything from helpers/
5. Now you have full context — write the complete fixed `tests/test_connector.py`
6. Run tests — iterate until all pass
7. Call done(summary)

## NEVER skip step 3. The client/ files tell you exactly which methods need AsyncMock.
If you skip reading them and guess, you WILL produce wrong mocks.

## CRITICAL: NEVER allow tests to make real API calls

Unit tests have NO credentials. Any test that reaches the real API fails with `ConnectionError`
or `401 Unauthorized`. ALWAYS verify the client is fully mocked before running tests.

### Checklist before writing any fixed test
- [ ] Is the connector's HTTP/SDK client class patched at `connector.XxxClient` BEFORE `__init__` runs?
- [ ] Does the `connector` fixture depend on the mock fixture (so patching happens first)?
- [ ] Does every test that invokes an API method set `mock_instance.method.return_value = {...}`?
- [ ] Are `side_effect` lists filled with plain dicts/values — NOT `AsyncMock(return_value=...)` wrappers?

### side_effect anti-pattern — causes silent real-API calls or Mock-cascade bugs
```python
# ❌ WRONG — awaiting returns the AsyncMock object, not the dict
mock_instance.get_status.side_effect = [AsyncMock(return_value={"STATUS": "PENDING"})]
# ✅ CORRECT
mock_instance.get_status.side_effect = [{"STATUS": "PENDING"}, {"STATUS": "TXN_SUCCESS"}]
```"""


_CONNECTOR_FIX_SYSTEM = """You are a Python expert fixing a Shielva connector package so that all tests pass.

## Your role
The tests in `tests/test_connector.py` define the expected behaviour.
Fix whatever files are needed — `connector.py`, `client/` files, `helpers/`, `__init__.py`, `exceptions.py`, or `tests/test_connector.py` — to make ALL tests pass.
Prefer fixing connector source files over modifying tests, but you MUST update tests when they assert wrong/buggy behavior (see PRIORITY 0 below).

## PRIORITY 0 — Tests asserting broken behavior (fix these BEFORE anything else)

Sometimes tests are written to DOCUMENT a known connector bug instead of the INTENDED behavior.
These tests can NEVER pass regardless of how you fix the connector — you MUST rewrite them.

### How to detect "bug-documenting" tests

1. **Test asserts an exception message as expected output**
   ```python
   assert "'str' object has no attribute 'get'" in result.message  # ← documents a crash
   assert "AttributeError" in result.message                       # ← documents a crash
   assert "NoneType" in result.message                             # ← documents a crash
   ```
   Real behavior should NEVER return an internal Python exception string as a user-facing message.
   Fix: rewrite the test to assert the INTENDED outcome (correct status, correct counts).

2. **`side_effect = [AsyncMock(return_value={...}), ...]` — wrong mock setup**
   Wrapping dict values in `AsyncMock()` inside a `side_effect` list is WRONG.
   When called, `await mock.method()` returns the `AsyncMock` object itself, not the dict.
   The connector then receives a Mock object instead of a dict → all `.get()` calls silently
   return new Mock objects → the test asserts the broken cascade behavior.
   Fix: use plain dicts directly in side_effect:
   ```python
   # WRONG
   mock.side_effect = [AsyncMock(return_value={"body": {...}}), ...]
   # CORRECT
   mock.side_effect = [{"body": {...}}, {"body": {...}}]
   ```

3. **Test assertions contradict correct connector logic with comments like**:
   - `# Changed from 1 to 0` (previous value was correct, changed to match bug)
   - `# documents_failed remains 0 due to connector bug`
   - `# Adjusted to match observed behavior`
   - `# Only one call before TypeError`
   These comments reveal the assertion was set to match a bug. Rewrite to assert intended behavior.

### How to rewrite bug-documenting tests
Read the connector method to understand what it SHOULD do (normal logic, not error path).
Then rewrite assertions to match that correct behavior. Examples:
- PENDING → SUCCESS retry test: should assert `status == SUCCESS`, `documents_synced == 1`, correct call count
- Max retries test: should assert `status == FAILED`, `documents_found == 1` (response received), correct retry count
- TXN_FAILURE test: connector received a response → `documents_found == 1`, `documents_failed == 1`
- Empty response test: no document found → `documents_found == 0`, `documents_failed == 1` (failure to get data)

## BaseConnector inherited methods (all REAL — do NOT redefine them)
```python
await self.save_config(config: Dict)    # merges config into self.config
await self.set_token(token_info)        # persists token
token = await self.get_token()          # returns Optional[TokenInfo]
await self.clear_token()                # clears token
await self.ingest_batch(docs, kb_id="") # ingests NormalizedDocument list
```

## PRIORITY 1 — Collection errors (fix these before anything else)

If pytest output contains `ImportError`, `ModuleNotFoundError`, or `ERROR collecting`:
→ These prevent ALL tests from running. Fix them first.

### ImportError: cannot import name 'X' from 'package.client'
Cause: connector.py uses an absolute package import but the test runner uses bare imports.
Fix IN connector.py — change the import:
  ❌ `from paytm_upi_connector.client import PaytmClient`
  ✅ `from client import PaytmClient`            ← bare import (cwd = connector package root)
  ✅ `from client.http_client import PaytmClient` ← explicit submodule

Also check `client/__init__.py` — if the class is defined in `client/http_client.py` but
`client/__init__.py` is empty or missing the export, add:
  `from .http_client import PaytmClient`

Read connector.py's import block FIRST before doing anything else when an ImportError appears.

### ModuleNotFoundError: No module named 'X'
Fix: add the missing package to `requirements.txt`, or correct the import path.

## PRIORITY 2 — Exception handler not catching (silent wrong-path bug)

### Symptom
`except SomeError` block never fires even though the test raises `SomeError`.
The exception falls through to `except Exception` instead, giving wrong behaviour
(e.g. returns `OFFLINE` instead of `UNHEALTHY`, or wrong message).

### Root cause — import path class identity mismatch
Python loads the same .py file under two different module names when the import
paths differ between connector.py and tests/test_connector.py.
Two classes from the same file but different import paths are NOT the same class:
  connector.py:          `from exceptions import PaytmAPIError`      → class id A
  test_connector.py:     `from pkg.exceptions import PaytmAPIError`  → class id B
`isinstance(e, A)` is False when e is class B → `except A` never catches it.

### How to detect it
Read both `connector.py` imports AND `tests/test_connector.py` imports.
If they import the same name from different paths (one bare, one package-prefixed), that IS the bug.
Example mismatch:
  ❌ connector.py:       `from exceptions import PaytmAPIError`
  ❌ test_connector.py:  `from paytm_upi_connector.exceptions import PaytmAPIError`

### Fix — make BOTH files use identical import paths
Change test_connector.py to match connector.py's bare import style:
  ✅ test_connector.py:  `from exceptions import PaytmAPIError`
  ✅ test_connector.py:  `from client import PaytmClient`
Do NOT change connector.py's imports (tests must match the module, not the other way round
unless connector.py is already using a wrong path).

Also check: if the custom exception class is missing `self.message = message` in `__init__`,
all `e.message` references in the connector will raise `AttributeError`:
  class FooError(Exception):
      def __init__(self, message, ...):
          super().__init__(message)
          self.message = message   ← must be here

## PRIORITY 3 — `.get()` called on a non-dict value

### Symptom
`AttributeError: 'str' object has no attribute 'get'`
`AttributeError: 'int' object has no attribute 'get'`

### Root cause
Code assumes a field is always a nested dict but the real API (and test mock) returns
a plain scalar.  Example:
  ❌ `body.get("txnAmount", {}).get("value")`  — crashes when txnAmount = "100.00"
  ✅ `_raw = body.get("txnAmount"); amount = _raw.get("value") if isinstance(_raw, dict) else _raw`

### Fix pattern
Wherever `.get(key, {}).get(subkey)` appears, guard with `isinstance(..., dict)`:
```python
_raw = body.get("txnAmount")
txn_amount = _raw.get("value") if isinstance(_raw, dict) else _raw
```

## Top connector failure patterns

1. `AttributeError: 'MyConnector' object has no attribute 'xxx'` —
   Fix: add `self.xxx = config.get("xxx", "")` in `configure()` or `__init__`.

2. `TypeError: object NoneType can't be used in await expression` —
   Fix: ensure the method is `async def`.

3. `AssertionError: expected X, got Y` on a return value —
   Fix: match the return value / data transformation to what the test asserts.

4. `ConnectorStatus` field mismatch — fields are `health`, `auth_status`, `connector_id`, `message`.
   Fix: replace `.status` with `.health` or `.auth_status` as appropriate.

5. `AttributeError: ... 'client'` — test patches a client attribute that doesn't match.
   Fix: make connector.py use exactly the same attribute name the test patches.

6. Wrong exception type raised — test uses `pytest.raises(SomeError)`.
   Fix: raise exactly that exception class on the expected code path.

7. `except SomeError` returns wrong health/status (e.g. OFFLINE instead of UNHEALTHY) —
   Always check PRIORITY 2 first: mismatched imports may mean the handler never fires.

## Workflow — follow this order exactly
1. Read the pytest output — identify whether it is a collection error or a test failure.
2. If collection error (ImportError/ModuleNotFoundError): read `connector.py` imports FIRST → fix them.
3. **Always** compare import statements between `connector.py` and `tests/test_connector.py`.
   If the same name is imported from different paths → PRIORITY 2 mismatch — fix test imports to match connector.py.
4. Read `client/` files and `exceptions.py` — check that custom exception classes store `self.message = message`.
5. Read `tests/test_connector.py` — understand what each test expects.
6. Scan for `.get(key, {}).get(subkey)` chains — verify the field is actually a dict in mocks/real API (PRIORITY 3).
7. Fix the necessary files (connector.py, exceptions.py, client/__init__.py, helpers/, or tests/).
8. Run tests — iterate until all pass.
9. Call done(summary).

## PRIORITY 4 — Tests making real API calls (no credentials provided)

Unit tests MUST NOT make real network calls. Tests that reach the real API will fail with
`ConnectionError`, `401 Unauthorized`, or hang indefinitely.

### How to detect it
- The connector's HTTP client class (e.g. `PaytmUpiClient`, `httpx.AsyncClient`) is NOT patched
  in the `connector` fixture — so `__init__` creates a real client instance.
- The `mock_XxxClient` fixture exists but is NOT listed as a parameter of the `connector` fixture,
  so pytest may build the connector before the patch activates.
- A method's mock is set up AFTER `connector` is constructed with a real client.

### Fix — ensure the client is patched BEFORE the connector is constructed
```python
# ✅ CORRECT — mock_PaytmUpiClient is patched first, connector.__init__ picks up the mock
@pytest.fixture
def mock_PaytmUpiClient(mocker):
    mock_cls = mocker.patch('connector.PaytmUpiClient', autospec=True)
    mock_instance = AsyncMock()
    mock_cls.return_value = mock_instance
    return mock_cls, mock_instance

@pytest.fixture
def connector(connector_config, mock_PaytmUpiClient):   # ← dependency listed explicitly
    return PaytmConnector(tenant_id="test-tenant", connector_id="test-id", config=connector_config)
```

Then in each test:
```python
async def test_health_check(self, connector, mock_PaytmUpiClient):
    _, mock_instance = mock_PaytmUpiClient
    mock_instance.check_wallet_balance.return_value = {"status": "SUCCESS", "statusCode": "00"}
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
```

### side_effect — NEVER wrap values in AsyncMock inside a list
```python
# ❌ WRONG — awaiting returns the AsyncMock object itself, not the dict
mock_instance.get_status.side_effect = [AsyncMock(return_value={"STATUS": "PENDING"}), ...]
# ✅ CORRECT — awaiting returns the dict directly
mock_instance.get_status.side_effect = [{"STATUS": "PENDING"}, {"STATUS": "TXN_SUCCESS"}]
```

## Rules
- Fix collection-level ImportErrors in connector.py BEFORE touching anything else.
- Use bare imports (`from client import X`) not package-prefixed (`from pkg.client import X`).
- When `except SomeError` seems to never fire → always check import path mismatch first.
- Preserve all existing connector methods — fix implementations, do not delete methods.
- Match attribute names, return types, and exception types exactly as tests expect.
- NEVER allow tests to make real network calls — ALL API client methods must be mocked."""


# ── Auth-type specific connector generation addenda ──────────────────────────
# These are appended to CONNECTOR_GEN_SYSTEM when auth_type is known.
# Stored in R2 at STEP_PROMPTS/CONNECTOR_GEN_SYSTEM_{auth_type}.txt
# If the R2 file exists it takes precedence; these are the seeded fallbacks.

_CONNECTOR_GEN_ADDENDUM_oauth2_code = """
### OAuth2 Authorization Code Flow — required implementation

**Required class constants** (copy exactly — missing → runtime error):
```python
AUTH_URI        = "https://provider.com/oauth/authorize"   # discovered from SDK docs
TOKEN_URI       = "https://provider.com/oauth/token"       # discovered from SDK docs
REQUIRED_SCOPES = ["scope1", "scope2"]                     # MUST use the EXACT scope strings from the provider's docs
```

**⚠ CRITICAL — REQUIRED_SCOPES must use the EXACT strings listed in the provider's API docs.**
Wrong scope names cause "scope not recognised" errors at runtime. Examples of correct scopes:
- Gmail full access: `"https://mail.google.com/"` (NOT `gmail.full`, NOT `gmail.readonly` unless read-only)
- Google Drive: `"https://www.googleapis.com/auth/drive"`
- Slack: `"channels:read"`, `"chat:write"` (space-separated short names, not URLs)
- GitHub: `"repo"`, `"user"` (short names)
- Notion: `""` (no scopes — uses workspace-level permissions)
Always use `REQUIRED_SCOPES` (not `SCOPES`) — the base class reads this attribute.

**Required methods:**
```python
async def install(self) -> ConnectorStatus:
    # Return PENDING — user must click Authorize next
    return ConnectorStatus(
        connector_id=self.connector_id,
        health=ConnectorHealth.OFFLINE,
        auth_status=AuthStatus.PENDING,
        message="Click Authorize to connect",
    )

async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
    # Exchange auth_code for tokens using TOKEN_URI
    # Store via self.set_token(token_info) — NEVER store in self.* vars
    # Return TokenInfo with access_token, refresh_token, expires_at, scope
    ...

async def health_check(self) -> ConnectorStatus:
    # Call self.ensure_token() to auto-refresh if expired
    # Return CONNECTED if token valid, TOKEN_EXPIRED if refresh fails
    ...
```

**Token refresh** — use `self.ensure_token()` (inherited) before every API call.
It checks expiry, calls TOKEN_URI with grant_type=refresh_token, and calls
`self.set_token()` automatically. Do NOT write your own refresh logic.

**Do NOT** hardcode client_id / client_secret — always `self.client_id = self.config.get("client_id", "")`.
"""

_CONNECTOR_GEN_ADDENDUM_oauth2_pkce = """
### OAuth2 PKCE Flow — required implementation

Same as oauth2_code PLUS PKCE challenge/verifier:
```python
# In get_oauth_url() (inherited) the platform generates code_verifier + code_challenge.
# The authorize() method receives the auth_code AND verifier via the callback.
async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
    # code_verifier is injected by the platform into the token exchange call.
    # Use self.ensure_token() for subsequent refresh — no custom refresh code.
    ...
```

Required class constants: AUTH_URI, TOKEN_URI, SCOPES (same as oauth2_code).
install() → PENDING, health_check() → CONNECTED/TOKEN_EXPIRED.
Never reimplement PKCE challenge logic — the platform handles it.
"""

_CONNECTOR_GEN_ADDENDUM_oauth2_client_credentials = """
### OAuth2 Client Credentials Flow — required implementation

No user authorization step. install() exchanges credentials directly for a token.

```python
async def install(self) -> ConnectorStatus:
    # POST to TOKEN_URI with grant_type=client_credentials
    # Store token via self.set_token(token_info)
    # Return CONNECTED on success, FAILED on error
    ...

async def health_check(self) -> ConnectorStatus:
    # Call self.ensure_token() — it handles expiry + re-fetch
    ...
```

**Do NOT** implement authorize() — there is no redirect flow.
Required: TOKEN_URI class constant. AUTH_URI is not needed.
"""

_CONNECTOR_GEN_ADDENDUM_api_key = """
### API Key Auth — required implementation

No token storage needed. The API key comes from self.config at runtime.

```python
async def install(self) -> ConnectorStatus:
    api_key = self.config.get("api_key", "")
    if not api_key:
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.MISSING_CREDENTIALS,
            message="API key is required",
        )
    # Optionally do a lightweight validation call (e.g. GET /me)
    # Return CONNECTED on success
    ...

async def health_check(self) -> ConnectorStatus:
    # Make a cheap read call with the api_key; return HEALTHY or OFFLINE
    ...
```

**Inject key in every request**: pass as header or query param per the SDK docs.
```python
headers = {"Authorization": f"Bearer {self.config['api_key']}"}  # or X-API-Key etc.
```

**Do NOT** implement authorize() — no redirect, no token refresh needed.
**Do NOT** call set_token() — api keys don't expire via OAuth.
"""

_CONNECTOR_GEN_ADDENDUM_service_account = """
### Service Account (JWT) Auth — required implementation

Credentials are a JSON blob stored in `self.config["service_account_json"]`.

```python
import json, time
import jwt  # PyJWT — already in requirements

async def install(self) -> ConnectorStatus:
    sa_json = json.loads(self.config.get("service_account_json", "{}"))
    # Build JWT assertion, exchange for access token at TOKEN_URI
    # Store access token via self.set_token(token_info) with expires_at
    ...

async def health_check(self) -> ConnectorStatus:
    token = await self.get_token()
    if not token or token.is_expired():
        # Re-mint JWT + exchange for new access token
        await self._refresh_service_account_token()
    ...
```

**Use self.set_token() / self.get_token()** for the short-lived access token.
Check `token.is_expired()` before each sync call and refresh if needed.
**Never** log or store the private key beyond the active request.
install_fields must include: `service_account_json` (type="textarea", required=true).
"""

_CONNECTOR_GEN_ADDENDUM_basic_auth = """
### Basic Auth — required implementation

Username + password from self.config, encoded as Base64 in every request header.

```python
import base64

async def install(self) -> ConnectorStatus:
    username = self.config.get("username", "")
    password = self.config.get("password", "")
    if not username or not password:
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.MISSING_CREDENTIALS,
        )
    # Optionally validate with a lightweight call
    ...

def _auth_headers(self) -> dict:
    creds = base64.b64encode(
        f"{self.config['username']}:{self.config['password']}".encode()
    ).decode()
    return {"Authorization": f"Basic {creds}"}
```

**Do NOT** implement authorize() — no redirect, no token refresh needed.
install_fields must include: `username` (type="text") and `password` (type="password").
"""

# Registry so sync_all can seed these to R2 / local cache on startup
_AUTH_TYPE_ADDENDA: Dict[str, str] = {
    "oauth2_code":                 _CONNECTOR_GEN_ADDENDUM_oauth2_code,
    "oauth2_pkce":                 _CONNECTOR_GEN_ADDENDUM_oauth2_pkce,
    "oauth2_client_credentials":   _CONNECTOR_GEN_ADDENDUM_oauth2_client_credentials,
    "api_key":                     _CONNECTOR_GEN_ADDENDUM_api_key,
    "service_account":             _CONNECTOR_GEN_ADDENDUM_service_account,
    "basic_auth":                  _CONNECTOR_GEN_ADDENDUM_basic_auth,
}


# ── Tool execution ────────────────────────────────────────────────────────────

def _safe_path(connector_dir: Path, relative_path: str) -> Path:
    """Resolve path, allowing connector_dir subtree and shared library reads."""
    # Allow absolute paths to shared library
    if os.path.isabs(relative_path):
        target = Path(relative_path).resolve()
        # Only allow reads from connectors root (shared lib)
        if str(target).startswith(str(_CONNECTORS_ROOT.resolve())):
            return target
        raise ValueError(f"Absolute path outside allowed scope: {relative_path}")
    target = (connector_dir / relative_path).resolve()
    if not str(target).startswith(str(connector_dir.resolve())):
        raise ValueError(f"Path traversal blocked: {relative_path}")
    return target


def _run_tests_sync(
    connector_dir: Path,
    failed_only: bool = False,
    methods: list[str] | None = None,
) -> str:
    """Run pytest in the connector directory.

    Args:
        failed_only: Use --lf to re-run only previously-failing tests (speedup
                     on 2nd+ iterations when full cache exists).
        methods:     When provided, add a -k expression so only tests for the
                     named methods run.  This is the critical fix — without it
                     Gemini sees failures from ALL methods when fixing just one.
    """
    abs_dir = connector_dir.resolve()
    pythonpath = os.pathsep.join(filter(None, [
        str(abs_dir), str(abs_dir.parent),
        _SITE_PACKAGES, _USER_SITE, str(_CONNECTORS_ROOT),
    ]))

    # -k expression: "health_check" or "install or health_check" etc.
    k_expr = " or ".join(methods) if methods else None

    if failed_only:
        cmd = [
            sys.executable, "-m", "pytest", "tests/",
            "-v", "--tb=short", "--no-header",
            "--lf", "--lfnf=all", "--maxfail=10",
        ]
    else:
        cmd = [
            sys.executable, "-m", "pytest", "tests/",
            "-v", "--tb=short", "--no-header",
        ]

    # Apply method filter AFTER deciding --lf vs full run
    if k_expr:
        cmd += ["-k", k_expr]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(abs_dir), capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": pythonpath}, timeout=120,
        )
        return (result.stdout + result.stderr).strip()[:8000]
    except subprocess.TimeoutExpired as _te:
        try:
            if _te.process:
                _te.process.kill()
        except Exception:
            pass
        return "TIMEOUT: pytest exceeded 120 seconds — too many or too slow tests"


async def _run_tests_async(
    connector_dir: Path,
    failed_only: bool = False,
    methods: list[str] | None = None,
) -> str:
    """Cancellable async version of _run_tests_sync.

    Creates the pytest subprocess *before* entering the thread so that
    ``asyncio.CancelledError`` (Stop button) can immediately ``proc.kill()``
    the process instead of waiting up to 120 s for it to finish.
    """
    abs_dir = connector_dir.resolve()
    _connectors_root = Path(__file__).resolve().parent.parent.parent
    _site = next(
        (p for p in sys.path if "site-packages" in p and Path(p).is_dir()), ""
    )
    pythonpath = os.pathsep.join(filter(None, [
        str(abs_dir),
        str(abs_dir.parent),
        _site,
        str(_connectors_root),
    ]))

    if failed_only:
        cmd = [
            sys.executable, "-m", "pytest", "tests/",
            "-v", "--tb=short", "--no-header",
            "--lf", "--lfnf=all", "--maxfail=10",
        ]
    else:
        cmd = [
            sys.executable, "-m", "pytest", "tests/",
            "-v", "--tb=short", "--no-header",
        ]

    if methods:
        cmd += ["-k", " or ".join(methods)]

    proc = subprocess.Popen(
        cmd,
        cwd=str(abs_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONPATH": pythonpath},
    )

    def _drain() -> str:
        try:
            out, _ = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return "TIMEOUT: pytest exceeded 120 seconds — too many or too slow tests"
        return (out or "").strip()[:8000]

    try:
        return await asyncio.to_thread(_drain)
    except asyncio.CancelledError:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise


def _execute_tool(
    name: str,
    args: Dict,
    connector_dir: Path,
    protected_files: set = None,
    target_methods: list[str] | None = None,
) -> str:
    if name == "read_file":
        try:
            if "path" not in args:
                return "ERROR: read_file requires 'path' argument"
            path = _safe_path(connector_dir, args["path"])
            return path.read_text(encoding="utf-8") if path.exists() else f"ERROR: File not found: {args['path']}"
        except Exception as e:
            return f"ERROR: {e}"

    elif name == "patch_file":
        # Surgical line replacement — only touches the exact lines specified.
        if "path" not in args:
            return "ERROR: patch_file requires 'path'"
        if "old_code" not in args or "new_code" not in args:
            return "ERROR: patch_file requires 'old_code' and 'new_code'"
        try:
            path = _safe_path(connector_dir, args["path"])
            if not path.exists():
                return f"ERROR: {args['path']} does not exist"
            content = path.read_text(encoding="utf-8")
            old_code = args["old_code"]
            new_code = args["new_code"]
            # Unescape Gemini over-escaped triple quotes in the patch too
            old_code = old_code.replace('\\"\\"\\"', '"""').replace("\\'\\'\\'" , "'''")
            new_code = new_code.replace('\\"\\"\\"', '"""').replace("\\'\\'\\'" , "'''")
            if old_code not in content:
                # Try with normalised line endings
                _normalised = content.replace("\r\n", "\n")
                _old_norm = old_code.replace("\r\n", "\n")
                if _old_norm in _normalised:
                    content = _normalised
                    old_code = _old_norm
                else:
                    # ── Truncation-aware fuzzy recovery ──────────────────────────────
                    # Gemini sometimes truncates old_code mid-string when the matched text
                    # is long (e.g. cuts "invalid token" → "invalid toke").
                    # Strategy 1: treat old_code as a prefix of a block in the file and
                    #   auto-extend to the nearest complete line boundary.
                    # Strategy 2: if old_code is multi-line and the last line was truncated,
                    #   match on the first non-empty line and extend by line count.
                    _recovered = False
                    _norm_content = _normalised
                    _prefix = _old_norm.rstrip()

                    # Strategy 1 — full prefix match (handles single-line truncation)
                    if len(_prefix) >= 10:
                        _idx = _norm_content.find(_prefix)
                        if _idx != -1:
                            _end_search = _idx + len(_prefix)
                            _eol = _norm_content.find("\n", _end_search)
                            _full_line_end = _eol + 1 if _eol != -1 else len(_norm_content)
                            old_code = _norm_content[_idx:_full_line_end]
                            content = _norm_content
                            _recovered = True

                    # Strategy 2 — first-line anchor (handles multi-line where last line is truncated)
                    if not _recovered:
                        _pfx_lines = [ln for ln in _prefix.split("\n") if ln.strip()]
                        if len(_pfx_lines) >= 2:
                            _anchor = _pfx_lines[0].rstrip()
                            if len(_anchor) >= 15:  # anchor must be non-trivial to avoid false matches
                                _fidx = _norm_content.find(_anchor)
                                if _fidx != -1:
                                    # Walk forward line-by-line for the same count as old_code had lines
                                    _num_lines = len(_old_norm.splitlines())
                                    _block_end = _fidx
                                    for _ in range(_num_lines):
                                        _next_nl = _norm_content.find("\n", _block_end)
                                        if _next_nl == -1:
                                            _block_end = len(_norm_content)
                                            break
                                        _block_end = _next_nl + 1
                                    old_code = _norm_content[_fidx:_block_end]
                                    content = _norm_content
                                    _recovered = True

                    if not _recovered:
                        return (
                            f"ERROR: patch_file could not find `old_code` in {args['path']}. "
                            "The text may have already been patched in a previous iteration, or your "
                            "old_code is truncated/incorrect. Read the file again, find the exact "
                            "current lines, and copy them verbatim including all whitespace/indentation."
                        )
            patched = content.replace(old_code, new_code, 1)
            path.write_text(patched, encoding="utf-8")
            # Run autoflake + ruff on patched file
            _patch_msg = f"OK: patched {args['path']} ({abs(len(new_code.splitlines()) - len(old_code.splitlines()))} line(s) changed)"
            if path.suffix == ".py":
                try:
                    from integration.services.code_quality import auto_fix_python_file
                    _qfix = auto_fix_python_file(path)
                    if _qfix["tools_applied"]:
                        _patch_msg += f" | auto-fixed ({', '.join(_qfix['tools_applied'])})"
                    if not _qfix["clean"]:
                        _patch_msg += f" | WARNING: syntax issue remains: {_qfix.get('syntax_error', 'unknown')}"
                except Exception:
                    pass
            return _patch_msg
        except Exception as e:
            return f"ERROR in patch_file: {e}"

    elif name == "write_file":
        try:
            if "path" not in args:
                return "ERROR: write_file requires 'path' argument (e.g. 'connector.py')"
            if "content" not in args:
                return "ERROR: write_file requires 'content' argument"
            # Protect specified files from being overwritten (e.g. connector.py during fix_tests).
            _write_path = args["path"].lstrip("/").lstrip("./")
            # Guard: during connector generation (protected_files is None = not fix_tests mode),
            # block test_*.py files written at the package root. Tests belong in tests/ only.
            if protected_files is None:
                import re as _re_path
                _wname = Path(_write_path).name
                _wparent = str(Path(_write_path).parent)
                if (_re_path.match(r"test_.*\.py$", _wname) or _wname.endswith("_test.py")) and _wparent == ".":
                    return (
                        f"ERROR: write_file('{_write_path}') BLOCKED — test files MUST go in tests/ subfolder, "
                        "not at the package root. Use 'tests/test_connector.py' or 'tests/test_client.py'. "
                        "❌ Do NOT write test files during connector generation — they are created by a separate step."
                    )
            # Match both exact path ("tests/test_connector.py") and bare filename
            # ("test_connector.py") — Gemini sometimes omits the tests/ prefix.
            _write_name = Path(_write_path).name
            _is_protected = protected_files and (
                _write_path in protected_files
                or any(Path(pf).name == _write_name for pf in protected_files)
            )
            if _is_protected:
                _can_write = "connector.py and client/ files" if any("test" in pf for pf in protected_files) else "tests/test_connector.py"
                return (
                    f"ERROR: write_file('{_write_path}') is BLOCKED — this file is read-only in the current mode. "
                    f"You may ONLY write to {_can_write}. "
                    "If the issue cannot be fixed without modifying this file, report it in done() summary."
                )
            path = _safe_path(connector_dir, args["path"])
            # ── Full-rewrite guard for connector.py ──────────────────────────────
            # During fix loops Gemini tends to rewrite the entire connector.py even
            # when only 1-2 methods need changing.  If >40% of lines are different
            # from the existing file, REJECT the write and force a targeted fix.
            # Exception: if the existing file is missing (new file) or very short
            # (stub), allow the write.
            _is_connector_py = Path(_write_path).name == "connector.py"
            if _is_connector_py and path.exists():
                import difflib as _difflib
                _existing_lines = path.read_text(encoding="utf-8").splitlines()
                _new_lines = args["content"].splitlines()
                if len(_existing_lines) > 50:  # only guard non-stub files
                    _matcher = _difflib.SequenceMatcher(None, _existing_lines, _new_lines, autojunk=False)
                    _ratio = _matcher.ratio()   # 0.0 = completely different, 1.0 = identical
                    if _ratio < 0.80:           # >20% of content changed → full rewrite detected
                        _changed_lines = sum(
                            abs(i2 - i1) + abs(j2 - j1)
                            for tag, i1, i2, j1, j2 in _matcher.get_opcodes()
                            if tag != "equal"
                        )
                        return (
                            f"BLOCKED: write_file('connector.py') rejected — too many changes "
                            f"(similarity={_ratio:.0%}, ~{_changed_lines} lines changed out of {len(_existing_lines)}). "
                            "You MUST make targeted, surgical fixes only — do NOT rewrite the whole file:\n"
                            "1. Read connector.py and find the EXACT lines that need changing.\n"
                            "2. Change ONLY those specific lines — keep everything else byte-for-byte identical.\n"
                            "3. A compile error (missing import, SyntaxError) is a 1-3 line fix at most.\n"
                            "4. A test assertion failure is a fix to ONE method body only.\n"
                            "Do NOT restructure, rename, reorder, or add docstrings to untouched methods."
                        )
            # ────────────────────────────────────────────────────────────────────
            path.parent.mkdir(parents=True, exist_ok=True)
            # ── Unescape Gemini over-escaped triple quotes ───────────────────
            # Gemini sometimes double-escapes docstring delimiters in its JSON
            # function-call output, writing `\"\"\"` literally into the file.
            # Python sees `\` as a line continuation character → SyntaxError:
            # "unexpected character after line continuation character".
            # Only fix the triple-quote patterns — leave single \" intact since
            # those are valid Python inside double-quoted strings.
            _content = args["content"]
            if path.suffix == ".py":
                _content = _content.replace('\\"\\"\\"', '"""').replace("\\'\\'\\'", "'''")
            path.write_text(_content, encoding="utf-8")
            result_msg = f"OK: wrote {len(_content)} chars to {args['path']}"
            # Deterministic post-fix for Python files: autoflake → ruff → ast.parse
            # Runs synchronously (no LLM). Cleans unused imports, indentation, etc.
            if path.suffix == ".py":
                try:
                    from integration.services.code_quality import auto_fix_python_file
                    fix = auto_fix_python_file(path)
                    if fix["tools_applied"]:
                        result_msg += f" | auto-fixed ({', '.join(fix['tools_applied'])})"
                    if not fix["clean"]:
                        result_msg += f" | WARNING: syntax issue remains: {fix.get('syntax_error', 'unknown')} — call validate_python to inspect"
                except Exception:
                    pass  # non-fatal — Gemini catches errors via validate_python / run_tests
            return result_msg
        except Exception as e:
            return f"ERROR: {e}"

    elif name == "validate_connector_rules":
        import re as _re_rules
        conn_path = connector_dir / "connector.py"
        if not conn_path.exists():
            return "ERROR: connector.py not found — write it first"
        src = conn_path.read_text(encoding="utf-8")
        violations = []

        # 1. stdlib logger instead of structlog
        if _re_rules.search(r'\blogging\.getLogger\b', src):
            violations.append(
                "VIOLATION: uses `logging.getLogger` — MUST use `import structlog; logger = structlog.get_logger(__name__)`. "
                "stdlib logger does NOT accept keyword arguments like logger.error('msg', field=value)."
            )

        # 2. install() calling API methods / health_check
        # Regex matches from "async def install(self)" up to the NEXT method definition at the
        # same indentation level (4-space-indented class methods), or end of class/file.
        # (?=\n    async def ) handles the common case (next class method).
        # (?=\n    def )       handles sync methods (e.g. __init__) that follow.
        # (?=\nclass )         handles a new top-level class.
        # (?=\nasync def )     handles a top-level async function (rare).
        # \Z                   fallback: last method in file.
        install_match = _re_rules.search(
            r'    async def install\(self\).*?(?=\n    (?:async )?def |\nclass |\nasync def |\Z)',
            src,
            _re_rules.DOTALL,
        )
        if install_match:
            install_body = install_match.group(0)
            bad_calls = _re_rules.findall(r'await self\.\w+\(', install_body)
            # allow: save_config, set_token, get_token, clear_token, ingest_batch
            allowed = {'save_config', 'set_token', 'get_token', 'clear_token', 'ingest_batch'}
            bad = [c for c in bad_calls if not any(a in c for a in allowed)]
            if bad:
                violations.append(
                    f"VIOLATION: install() makes API/network calls: {bad}. "
                    "install() MUST only validate self.config keys and return ConnectorStatus. "
                    "The gateway calls health_check() separately after install(). "
                    "REMOVE the API call from install() entirely."
                )

        # 3. Wrong ConnectorStatus field: .status doesn't exist
        if _re_rules.search(r'\.status\s*==\s*ConnectorStatus\b', src) or \
           _re_rules.search(r'ConnectorStatus\.[A-Z_]+\b(?!\()', src):
            # Check for ConnectorStatus used as an enum value (it's a class, not an enum)
            bad_uses = _re_rules.findall(r'ConnectorStatus\.(SUCCESS|FAILED|PENDING|UNKNOWN|OK|PASS)\b', src)
            if bad_uses:
                violations.append(
                    f"VIOLATION: ConnectorStatus.{bad_uses[0]} doesn't exist. "
                    "ConnectorStatus is a dataclass with fields: connector_id, health, auth_status, message. "
                    "Use ConnectorHealth.HEALTHY/UNHEALTHY/OFFLINE/DEGRADED and AuthStatus.CONNECTED/etc."
                )

        # 4. os.getenv / os.environ for credentials
        if _re_rules.search(r'os\.getenv\(|os\.environ\.get\(', src):
            violations.append(
                "VIOLATION: uses os.getenv/os.environ — ALL credentials must come from self.config.get(key). "
                "NEVER read credentials from environment variables in the connector."
            )

        # 5. Missing connector_id in ConnectorStatus
        status_calls = _re_rules.findall(r'ConnectorStatus\((?![^)]*connector_id)', src)
        if status_calls:
            violations.append(
                f"VIOLATION: {len(status_calls)} ConnectorStatus() call(s) missing connector_id=self.connector_id. "
                "connector_id is REQUIRED — missing it causes TypeError at runtime."
            )

        # 6. Relative imports
        if _re_rules.search(r'from \.(connector|shared|base)', src):
            violations.append(
                "VIOLATION: relative import found. Use `from shared.base_connector import ...` — never `from .shared import ...`"
            )

        # 7. Custom exception missing self.message
        # Pattern: class FooError ... def __init__(self, message ...) with super().__init__(message) but no self.message =
        for exc_match in _re_rules.finditer(
            r'class\s+(\w+Error)\b.*?def __init__\(self,\s*message\b.*?\n(.*?)(?=\n    def |\nclass |\Z)',
            src, _re_rules.DOTALL
        ):
            init_body = exc_match.group(2)
            if 'super().__init__(message)' in init_body and 'self.message' not in init_body:
                violations.append(
                    f"VIOLATION: {exc_match.group(1)}.__init__ calls super().__init__(message) but never sets "
                    f"self.message = message. Connector code accesses e.message — add `self.message = message` "
                    f"after super().__init__(message)."
                )

        # 8. super().disconnect() — BaseConnector has no disconnect() method
        if _re_rules.search(r'await\s+super\(\)\.disconnect\(\)', src):
            violations.append(
                "VIOLATION: `await super().disconnect()` — BaseConnector has NO disconnect() method. "
                "Remove this line. Just close self._client and set self._client = None."
            )

        # 9. PaymentsConnectorConfig(**self.config) without uppercase key mapping
        # self.config stores lowercase keys (client_id) but pydantic fields are uppercase (CLIENT_ID)
        if _re_rules.search(r'ConnectorConfig\(\*\*self\.config\)', src):
            violations.append(
                "VIOLATION: `SomeConnectorConfig(**self.config)` — self.config has lowercase keys (e.g. client_id) "
                "but pydantic-settings fields are uppercase (CLIENT_ID). Use: "
                "`SomeConnectorConfig(**{k.upper(): v for k, v in self.config.items()})` instead."
            )

        # 10. httpx.TimeoutException swallowed inside get_transaction_status / _request
        # If there's a bare `except Exception` that wraps TimeoutException before callers see it,
        # health_check can't return OFFLINE. Check if TimeoutException is explicitly re-raised or
        # caught before the generic handler in methods that callers rely on.
        # (Lightweight heuristic — only flag if TimeoutException is NOT mentioned at all)
        if _re_rules.search(r'async def get_transaction_status', src):
            gts_match = _re_rules.search(
                r'async def get_transaction_status.*?(?=\n    async def |\nclass |\Z)', src, _re_rules.DOTALL
            )
            if gts_match:
                gts_body = gts_match.group(0)
                if 'except Exception' in gts_body and 'TimeoutException' not in gts_body:
                    violations.append(
                        "VIOLATION: get_transaction_status() has `except Exception` that will swallow "
                        "httpx.TimeoutException before health_check() can catch it. Add "
                        "`except httpx.TimeoutException: raise` BEFORE the generic `except Exception` handler "
                        "so health_check can return OFFLINE on timeout."
                    )

        # 11. Missing CONNECTOR_TYPE class attribute
        _ct_match_rules = _re_rules.search(r'CONNECTOR_TYPE\s*=\s*["\']([^"\']+)["\']', src)
        if not _ct_match_rules:
            violations.append(
                "VIOLATION: CONNECTOR_TYPE class attribute is missing. "
                "Every connector class MUST define it as a class-level constant, e.g.: "
                "  CONNECTOR_TYPE = \"gmail\"   # lowercase snake_case, no _connector suffix "
                "The gateway uses this attribute to register and load the connector — "
                "without it POST /connectors/deploy returns 404 'Connector type not found'."
            )
        else:
            _ct_val = _ct_match_rules.group(1)
            # 12. CONNECTOR_TYPE naming convention: lowercase snake_case, no _connector suffix, no spaces/hyphens
            if _ct_val != _ct_val.lower():
                violations.append(
                    f"VIOLATION: CONNECTOR_TYPE = \"{_ct_val}\" is not all-lowercase. "
                    f"Use lowercase snake_case: CONNECTOR_TYPE = \"{_ct_val.lower()}\""
                )
            elif "-" in _ct_val or " " in _ct_val:
                violations.append(
                    f"VIOLATION: CONNECTOR_TYPE = \"{_ct_val}\" contains hyphens or spaces. "
                    f"Use underscores: CONNECTOR_TYPE = \"{_ct_val.replace('-', '_').replace(' ', '_')}\""
                )
            elif _ct_val.endswith("_connector"):
                violations.append(
                    f"VIOLATION: CONNECTOR_TYPE = \"{_ct_val}\" must NOT end with '_connector'. "
                    f"Standard: just the service slug — e.g. \"gmail\" not \"gmail_connector\". "
                    f"Fix: CONNECTOR_TYPE = \"{_ct_val[:-len('_connector')]}\""
                )

        # 13. Package-prefixed subpackage imports in connector.py
        # e.g. `from gmail_connector.client.http_client import X` — WRONG
        # Gateway adds connector dir to sys.path; use `from client.http_client import X`
        _pkg_prefix_imports = _re_rules.findall(
            r'from\s+\w+_connector\.(client|helpers|repository)\b',
            src,
        )
        if _pkg_prefix_imports:
            violations.append(
                f"VIOLATION: {len(_pkg_prefix_imports)} package-prefixed subpackage import(s) in connector.py "
                f"(e.g. `from xxx_connector.client.http_client import ...`). "
                "The gateway adds the connector's own directory to sys.path — use bare imports: "
                "`from client.http_client import XxxClient` or `from helpers import xxx_utils`. "
                "Package-prefixed imports cause ImportError / ModuleNotFoundError at load time."
            )

        # ── SRP / OCP compliance scoring ─────────────────────────────────────
        _srp_score = 5
        _ocp_score = 5
        _srp_viols: list = []
        _ocp_viols: list = []

        # SRP-1: data transformation methods in connector.py (>6 lines → belongs in helpers/)
        _dt_methods = list(_re_rules.finditer(
            r'    def (_parse_\w+|_normalize_\w+|_map_\w+|_transform_\w+|_extract_\w+)\(self',
            src,
        ))
        for _dtm in _dt_methods:
            _rest = src[_dtm.end():]
            _nx = _re_rules.search(r'\n    (?:async )?def ', _rest)
            _lines = (_rest[: _nx.start()] if _nx else _rest).count("\n")
            if _lines > 6:
                _srp_score -= 1
                _srp_viols.append(
                    f"SRP-1: `{_dtm.group(1)}()` ({_lines} lines) is data transformation — "
                    "move to `helpers/` (e.g. helpers/data_utils.py). "
                    "connector.py should coordinate, not transform."
                )
                break  # one deduction per SRP-1

        # SRP-2: message/payload construction helpers in connector.py
        _mc = _re_rules.search(
            r'    def (_create_message|_build_message|_encode_message|_build_payload|_build_request|_format_request)\(',
            src,
        )
        if _mc:
            _srp_score -= 1
            _srp_viols.append(
                f"SRP-2: `{_mc.group(1)}()` is message/payload construction — "
                "move to `helpers/` (not connector coordination logic)."
            )

        # SRP-3 & SRP-4 & SRP-5: checks against client/http_client.py
        _http_client_path = connector_dir / "client" / "http_client.py"
        if _http_client_path.exists():
            _cl_src = _http_client_path.read_text(encoding="utf-8")

            # SRP-3: OAuth flow in client
            if _re_rules.search(r'def get_flow|fetch_token|exchange_code|from_client_config', _cl_src):
                _srp_score -= 1
                _srp_viols.append(
                    "SRP-3: client/http_client.py contains OAuth flow code (get_flow/fetch_token). "
                    "OAuth is connector.py's responsibility — remove it from the client."
                )

            # SRP-4: duplicate token refresh in both files
            _conn_refresh = bool(_re_rules.search(r'grant_type.*refresh_token|refresh_token.*grant_type', src))
            _cl_refresh = bool(_re_rules.search(
                r'grant_type.*refresh_token|refresh_token.*grant_type|credentials\.refresh\(', _cl_src
            ))
            if _conn_refresh and _cl_refresh:
                _srp_score -= 1
                _srp_viols.append(
                    "SRP-4: Token refresh duplicated in both connector.py and client/http_client.py. "
                    "Only on_token_refresh() in connector.py should refresh — remove refresh logic from client."
                )

            # SRP-5: data transformation in client
            _cl_dt = _re_rules.search(r'    def (_normalize_\w+|_parse_\w+|_map_\w+|_transform_\w+)', _cl_src)
            if _cl_dt:
                _srp_score -= 1
                _srp_viols.append(
                    f"SRP-5: client/http_client.py has `{_cl_dt.group(1)}()` — "
                    "data transformation belongs in helpers/, not in the HTTP client."
                )

        # OCP-1: health_check status-code if/elif chain (≥2 branches → use _STATUS_MAP)
        # Catches all common patterns: status_code ==, e.resp.status ==, resp.status ==,
        # response.status_code ==, e.status_code ==
        _hc_m = _re_rules.search(
            r'async def health_check.*?(?=\n    (?:async )?def |\nclass |\Z)', src, _re_rules.DOTALL
        )
        if _hc_m:
            _hc_body = _hc_m.group(0)
            _sc_pattern = (
                r'(?:if|elif)\s+'
                r'(?:status_code|e\.resp\.status|resp\.status|e\.status_code|'
                r'response\.status_code|err\.resp\.status|error\.resp\.status)'
                r'\s*==\s*\d+'
            )
            _sc_branches = len(_re_rules.findall(_sc_pattern, _hc_body))
            if _sc_branches >= 2 and not _re_rules.search(r'_STATUS_MAP|STATUS_MAP', src):
                _ocp_score -= 1
                _ocp_viols.append(
                    f"OCP-1: health_check() has {_sc_branches} status-code if/elif branches "
                    "(matched pattern: status_code/e.resp.status/response.status_code). "
                    "Replace with a class-level dict: "
                    "_STATUS_MAP = {401: (ConnectorHealth.OFFLINE, AuthStatus.TOKEN_EXPIRED, 'Token expired'), "
                    "403: (ConnectorHealth.UNHEALTHY, AuthStatus.INVALID_CREDENTIALS, 'Forbidden'), "
                    "429: (ConnectorHealth.DEGRADED, AuthStatus.CONNECTED, 'Rate limited')} "
                    "and look up: health, auth, msg = _STATUS_MAP.get(code, (UNHEALTHY, FAILED, f'Error {code}'))."
                )
            # OCP-1 blind spot: health_check() exists but _STATUS_MAP is completely absent (zero branches too)
            if not _re_rules.search(r'_STATUS_MAP|STATUS_MAP', src):
                _ocp_score -= 1
                _ocp_viols.append(
                    "OCP-1: health_check() exists but _STATUS_MAP class attribute is completely absent. "
                    "Add: _STATUS_MAP = {401: (ConnectorHealth.OFFLINE, AuthStatus.TOKEN_EXPIRED, 'Token expired'), "
                    "403: (ConnectorHealth.UNHEALTHY, AuthStatus.INVALID_CREDENTIALS, 'Forbidden'), "
                    "429: (ConnectorHealth.DEGRADED, AuthStatus.CONNECTED, 'Rate limited')} "
                    "and look up: health, auth, msg = self._STATUS_MAP.get(code, (ConnectorHealth.UNHEALTHY, AuthStatus.FAILED, f'Error {code}'))."
                )

        # OCP-2: MIME type if/elif chain (≥2 branches → use MIME_PRIORITY list)
        # Scans connector.py AND helpers/*.py so helpers-side violations are caught too.
        _mime_src_parts = [src]
        _helpers_dir = connector_dir / "helpers"
        if _helpers_dir.is_dir():
            for _hf in _helpers_dir.glob("*.py"):
                try:
                    _mime_src_parts.append(_hf.read_text(encoding="utf-8"))
                except Exception:
                    pass
        _mime_combined = "\n".join(_mime_src_parts)
        _mime_count = len(_re_rules.findall(
            r'(?:if|elif)\s+(?:mime_type|mimeType|part\[["\'"]mimeType["\'"\]|part\.get\(["\']mimeType["\']|mime)\s*(?:==|\.startswith\()',
            _mime_combined,
        ))
        _has_mime_priority = _re_rules.search(
            r'MIME_PRIORITY|mime_priority|CONTENT_PRIORITY', _mime_combined
        )
        if _mime_count >= 2 and not _has_mime_priority:
            _ocp_score -= 1
            _ocp_viols.append(
                f"OCP-2: {_mime_count} MIME type if/elif branches. "
                "Replace with: MIME_PRIORITY = ['text/plain', 'text/html'] "
                "and iterate: for mime in MIME_PRIORITY: ... (extend list, never modify the loop)."
            )

        # OCP-3: Required config keys inline in install() (>2 keys → use REQUIRED_CONFIG_KEYS class constant)
        _install_body_ocp = install_match.group(0) if install_match else ""
        _inline_req = _re_rules.search(r'required\s*=\s*\[([^\]]{25,})\]', _install_body_ocp)
        if _inline_req and not _re_rules.search(r'REQUIRED_CONFIG_KEYS\s*=', src):
            _key_count = _inline_req.group(1).count('"') // 2 + _inline_req.group(1).count("'") // 2
            if _key_count > 2:
                _ocp_score -= 1
                _ocp_viols.append(
                    f"OCP-3: install() has {_key_count} required keys as an inline list. "
                    "Define as a class constant: REQUIRED_CONFIG_KEYS = [...] "
                    "then: missing = [k for k in self.REQUIRED_CONFIG_KEYS if not self.config.get(k)]."
                )

        # OCP-4: ≥4 consecutive elif branches on same variable (should be a dict)
        _elif_chains = _re_rules.findall(
            r'\bif\s+(\w+)\s*==\s*[^\n:]+:(?:\s*\n[^\n]*){0,6}?\n\s*(?:elif\s+\1\s*==\s*[^\n:]+:\s*\n[^\n]*){3,}',
            src,
        )
        if _elif_chains and not _re_rules.search(r'_MAP\s*=\s*\{|_map\s*=\s*\{', src):
            _ocp_score -= 1
            _ocp_viols.append(
                f"OCP-4: Long if/elif chain (4+ branches) on `{_elif_chains[0]}` found. "
                "Replace with a lookup dict so new cases can be added without modifying the dispatch logic."
            )

        # OCP-5: hardcoded asyncio.sleep values (≥2 → use class constants)
        _sleep_vals = _re_rules.findall(r'asyncio\.sleep\(\s*\d+(?:\.\d+)?\s*\)', src)
        if len(_sleep_vals) >= 2 and not _re_rules.search(r'RETRY_DELAY_S|BACKOFF|retry_delay', src):
            _ocp_score -= 1
            _ocp_viols.append(
                f"OCP-5: {len(_sleep_vals)} hardcoded asyncio.sleep() values. "
                "Use class constants: RETRY_DELAY_S = 1.0; BACKOFF_FACTOR = 2.0 "
                "so retry timing can be tuned without modifying logic."
            )

        # ── Compliance gate ───────────────────────────────────────────
        _total_score = _srp_score + _ocp_score  # out of 10
        _score_lines = [
            f"\n── SRP/OCP Compliance: {_total_score}/10 ──",
            f"  SRP: {_srp_score}/5  |  OCP: {_ocp_score}/5",
        ]
        if _srp_viols:
            _score_lines.append("  SRP violations:")
            for _sv in _srp_viols:
                _score_lines.append(f"    • {_sv}")
                violations.append(_sv)
        if _ocp_viols:
            _score_lines.append("  OCP violations:")
            for _ov in _ocp_viols:
                _score_lines.append(f"    • {_ov}")
                violations.append(_ov)
        if _total_score < 9:
            violations.append(
                f"COMPLIANCE GATE: SRP/OCP score is {_total_score}/10 — minimum 9/10 required. "
                "Fix the SRP/OCP violations above before calling done()."
            )

        _score_summary = "\n".join(_score_lines)

        if violations:
            return (
                "VIOLATIONS FOUND — fix all before calling done():\n"
                + "\n".join(f"  • {v}" for v in violations)
                + _score_summary
            )
        return f"OK — no rule violations found in connector.py{_score_summary}"

    elif name == "run_smoke_test":
        import sys as _sys, os as _os, subprocess as _sp, tempfile as _tf, textwrap as _tw, json as _json_ag

        conn_path = connector_dir / "connector.py"
        if not conn_path.exists():
            return "ERROR: connector.py not found — write it first"

        # Build smoke config dynamically from connector metadata
        _smoke_config_ag: dict = {}
        _meta_path_ag = connector_dir / "metadata" / "connector.json"
        if _meta_path_ag.exists():
            try:
                _meta_ag = _json_ag.loads(_meta_path_ag.read_text(encoding="utf-8"))
                for _field_ag in _meta_ag.get("install_fields", []):
                    _key_ag = _field_ag.get("key", "")
                    if not _key_ag:
                        continue
                    if _field_ag.get("type") == "json" or "json" in _key_ag.lower():
                        _smoke_config_ag[_key_ag] = '{"type":"service_account"}'
                    else:
                        _smoke_config_ag[_key_ag] = "test"
            except Exception:
                pass
        if not _smoke_config_ag:
            _smoke_config_ag = {
                "api_key": "test", "client_id": "test", "client_secret": "test",
                "username": "test", "password": "test",
                "service_account_json": '{"type":"service_account"}',
            }
        _smoke_config_ag_repr = repr(_smoke_config_ag)

        # Write a self-contained smoke test script that runs in a subprocess
        # so it gets a clean import namespace and proper PYTHONPATH
        smoke_script = _tw.dedent("""
        import sys, os, asyncio, traceback
        from unittest.mock import AsyncMock, MagicMock, patch

        # Patch structlog before importing connector so it doesn't need a real logging setup
        import structlog
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))

        # Patch httpx.AsyncClient globally so no real network calls happen
        import httpx
        _mock_response = MagicMock()
        _mock_response.status_code = 200
        _mock_response.json.return_value = {"resultInfo": {"resultStatus": "S"}, "status": "SUCCESS"}
        _mock_response.raise_for_status = MagicMock()
        _mock_client_instance = AsyncMock()
        _mock_client_instance.request = AsyncMock(return_value=_mock_response)
        _mock_client_instance.post = AsyncMock(return_value=_mock_response)
        _mock_client_instance.get = AsyncMock(return_value=_mock_response)
        _mock_client_instance.__aenter__ = AsyncMock(return_value=_mock_client_instance)
        _mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        results = []

        with patch('httpx.AsyncClient', return_value=_mock_client_instance):
            try:
                # Import as a package (connector_dir.parent is in PYTHONPATH) so that
                # relative imports like `from .client import X` work correctly.
                # Falls back to direct `import connector` for connectors that use
                # only absolute imports.
                import importlib as _il, os as _os3
                _pkg_name = _os3.path.basename(_os3.getcwd())
                try:
                    _conn_mod = _il.import_module(f"{_pkg_name}.connector")
                except Exception:
                    import connector as _conn_mod
                # Find the connector class (subclass of BaseConnector)
                from shared.base_connector import BaseConnector
                cls = next(
                    (v for v in vars(_conn_mod).values()
                     if isinstance(v, type) and issubclass(v, BaseConnector) and v is not BaseConnector),
                    None
                )
                if not cls:
                    print("FAIL: no BaseConnector subclass found in connector.py")
                    sys.exit(1)
                results.append(f"PASS: class {cls.__name__} found")
            except Exception as e:
                print(f"FAIL: import error — {traceback.format_exc()}")
                sys.exit(1)

            # Instantiate with mock config (keys from connector metadata)
            try:
                inst = cls(
                    tenant_id="smoke-tenant",
                    connector_id="smoke-connector",
                    config=__SMOKE_CONFIG__,
                )
                results.append(f"PASS: {cls.__name__}() instantiated")
            except Exception as e:
                print(f"FAIL: instantiation — {traceback.format_exc()}")
                sys.exit(1)

            # Patch BaseConnector storage methods so no real Redis/DB calls
            with patch.object(cls, 'save_config', new_callable=lambda: lambda *a, **kw: AsyncMock(return_value=None)() or AsyncMock(return_value=None)), \
                 patch.object(cls, 'get_token', new_callable=lambda: lambda *a, **kw: AsyncMock(return_value=None)() or AsyncMock(return_value=None)), \
                 patch.object(cls, 'set_token', new_callable=lambda: lambda *a, **kw: AsyncMock(return_value=None)() or AsyncMock(return_value=None)):
                pass  # just check patches work

            # Run install()
            try:
                async def _run_install():
                    with patch.object(cls, 'save_config', AsyncMock(return_value=None)), \\
                         patch.object(cls, 'get_token', AsyncMock(return_value=None)), \\
                         patch.object(cls, 'set_token', AsyncMock(return_value=None)):
                        return await inst.install()
                result = asyncio.run(_run_install())
                # Check result has required fields
                if not hasattr(result, 'health'):
                    print(f"FAIL: install() returned {type(result)} — expected ConnectorStatus with .health field")
                    sys.exit(1)
                if not hasattr(result, 'connector_id') or not result.connector_id:
                    print(f"FAIL: install() ConnectorStatus missing connector_id — add connector_id=self.connector_id")
                    sys.exit(1)
                results.append(f"PASS: install() returned ConnectorStatus(health={result.health}, auth_status={result.auth_status})")
            except TypeError as e:
                if "unexpected keyword argument" in str(e):
                    print(f"FAIL: install() logger error — {e}\\nFix: use structlog not logging.getLogger")
                else:
                    print(f"FAIL: install() TypeError — {traceback.format_exc()}")
                sys.exit(1)
            except Exception as e:
                print(f"FAIL: install() — {traceback.format_exc()}")
                sys.exit(1)

        for r in results:
            print(r)
        print("SMOKE TEST PASSED")
        """).replace("__SMOKE_CONFIG__", _smoke_config_ag_repr)

        try:
            with _tf.NamedTemporaryFile(mode='w', suffix='.py', delete=False, dir=str(connector_dir)) as f:
                f.write(smoke_script)
                tmp_path = f.name

            env = {**__import__('os').environ}
            # PYTHONPATH needs:
            #   connector_dir.parent — so the connector can be imported as a package
            #                          (enables relative imports: `from .client import X`)
            #   connector_dir        — fallback for connectors using absolute sub-imports
            #   _CONNECTORS_ROOT     — so `from shared.base_connector import ...` resolves
            existing_pp = env.get('PYTHONPATH', '')
            env['PYTHONPATH'] = (
                f"{str(connector_dir.parent)}{_os.pathsep}"
                f"{str(connector_dir)}{_os.pathsep}"
                f"{str(_CONNECTORS_ROOT)}{_os.pathsep}{existing_pp}"
            )

            proc = _sp.run(
                [_sys.executable, tmp_path],
                capture_output=True, text=True, timeout=30, env=env,
                cwd=str(connector_dir),
            )
            __import__('os').unlink(tmp_path)

            output = (proc.stdout + proc.stderr).strip()
            if proc.returncode == 0 and "SMOKE TEST PASSED" in output:
                lines = [l for l in output.splitlines() if l.strip()]
                return "SMOKE TEST PASSED:\n" + "\n".join(f"  ✓ {l}" for l in lines if l != "SMOKE TEST PASSED")
            else:
                return f"SMOKE TEST FAILED — fix the issue then re-run:\n{output}"
        except _sp.TimeoutExpired:
            return "SMOKE TEST FAILED: timed out (30s) — install() is likely making real network calls or blocking. Check install() only validates config keys."
        except Exception as e:
            return f"SMOKE TEST ERROR: {e}"

    elif name == "validate_python":
        try:
            path = _safe_path(connector_dir, args["path"])
            if not path.exists():
                return f"ERROR: File not found: {args['path']}"
            source = path.read_text(encoding="utf-8")
            line_count = len(source.splitlines())
            _ast.parse(source)
            # Warn on suspiciously short files — empty or near-empty files cause import errors at runtime.
            # __init__.py files especially must export their classes, not be empty placeholders.
            _fname = Path(args["path"]).name
            if line_count == 0:
                return (
                    f"WARNING: {args['path']} is EMPTY (0 lines). "
                    f"An empty file is syntactically valid but will cause ImportError at runtime if "
                    f"connector.py tries to import from it. "
                    f"If this is client/__init__.py, add: `from .http_client import <YourClientClass>`"
                )
            if line_count <= 2 and _fname == "__init__.py":
                return (
                    f"WARNING: {args['path']} has only {line_count} line(s) — likely missing the class export. "
                    f"Add: `from .http_client import <YourClientClass>` so connector.py can import it."
                )
            return f"OK: {args['path']} is valid Python ({line_count} lines)"
        except SyntaxError as e:
            return f"SyntaxError at line {e.lineno}: {e.msg}"
        except ValueError as e:
            return f"ERROR: {e}"

    elif name == "check_imports":
        # Try to actually import the full connector package — catches NameError, ImportError,
        # missing typing imports, wrong attribute names, etc. that ast.parse misses.
        import subprocess as _sp, sys as _sys, os as _os
        # PYTHONPATH needs:
        #   1. connector_dir itself (so `import exceptions` resolves)
        #   2. repo_root (shielva-connectors/) so `from shared.base_connector import ...` resolves
        _repo_root = Path(settings.GENERATED_CODE_DIR).resolve().parent
        pythonpath = _os.pathsep.join([str(connector_dir), str(_repo_root), str(connector_dir.parent)])
        _check_script = (
            "import sys, pathlib, py_compile, traceback, importlib, typing, inspect\n"
            "sys.path.insert(0, '.')\n"
            "cwd = pathlib.Path('.')\n"
            # Collect all .py files: top-level + subdirs (client/, helpers/, etc.) excluding tests/__pycache__
            "py_files = sorted(\n"
            "    f for f in cwd.rglob('*.py')\n"
            "    if '__pycache__' not in f.parts and 'tests' not in f.parts\n"
            "    and not f.name.startswith('test_')\n"
            ")\n"
            "errors = []\n"
            # ── Phase 1: compile every file — catches SyntaxError fast ──────────
            "for py_file in py_files:\n"
            "    try:\n"
            "        py_compile.compile(str(py_file), doraise=True)\n"
            "    except py_compile.PyCompileError as e:\n"
            "        errors.append(f'SyntaxError in {py_file}: {e}')\n"
            # ── Phase 2: import top-level modules + force annotation eval ────────
            "if not errors:\n"
            "    top_mods = sorted(\n"
            "        f.stem for f in cwd.glob('*.py')\n"
            "        if f.stem != '__init__' and not f.stem.startswith('test_')\n"
            "    )\n"
            "    for mod_name in top_mods:\n"
            "        try:\n"
            "            if mod_name in sys.modules: del sys.modules[mod_name]\n"
            "            mod = importlib.import_module(mod_name)\n"
            "        except Exception as e:\n"
            "            errors.append(f'{mod_name}: {type(e).__name__}: {e}')\n"
            "            errors.append(traceback.format_exc().strip())\n"
            "            continue\n"
            # Force annotation evaluation — catches missing Optional/List/Dict etc.
            # that PEP 649 (Python 3.14) defers until annotations are accessed.
            "        for attr_name in dir(mod):\n"
            "            try:\n"
            "                obj = getattr(mod, attr_name)\n"
            "                if callable(obj) and (inspect.isfunction(obj) or inspect.isclass(obj)):\n"
            "                    typing.get_type_hints(obj)\n"
            "            except Exception as e:\n"
            "                errors.append(f'{mod_name}.{attr_name}: {type(e).__name__}: {e}')\n"
            "if errors:\n"
            "    print('IMPORT ERRORS FOUND:'); [print(e) for e in errors]\n"
            "else:\n"
            "    print('OK: all imports clean')\n"
        )
        try:
            _res = _sp.run(
                [_sys.executable, "-c", _check_script],
                cwd=str(connector_dir),
                capture_output=True, text=True, timeout=15,
                env={**__import__("os").environ, "PYTHONPATH": pythonpath},
            )
            out = (_res.stdout + _res.stderr).strip()
            return out if out else "OK: all imports clean"
        except Exception as e:
            return f"ERROR running check_imports: {e}"

    elif name == "validate_json":
        try:
            path = _safe_path(connector_dir, args["path"])
            if not path.exists():
                return f"ERROR: File not found: {args['path']}"
            data = json.loads(path.read_text(encoding="utf-8"))
            keys = list(data.keys()) if isinstance(data, dict) else f"array[{len(data)}]"
            return f"OK: valid JSON. Top-level keys: {keys}"
        except json.JSONDecodeError as e:
            return f"JSONDecodeError: {e}"
        except ValueError as e:
            return f"ERROR: {e}"

    elif name == "list_files":
        files = sorted(f.relative_to(connector_dir) for f in connector_dir.rglob("*") if f.is_file())
        return "\n".join(str(f) for f in files)

    elif name == "run_tests":
        # After the first run a .pytest_cache exists — subsequent calls use
        # --lf (last-failed) so only previously failing tests re-run.
        # target_methods adds -k filter so only the methods being fixed are tested —
        # this prevents Gemini from seeing failures from OTHER methods and going off-track.
        cache_dir = connector_dir / ".pytest_cache" / "v" / "cache" / "lastfailed"
        failed_only = cache_dir.exists()
        return _run_tests_sync(connector_dir, failed_only=failed_only, methods=target_methods)

    elif name == "search_knowledge":
        # search_knowledge is async — handled in the loop, not here
        return "ERROR: search_knowledge must be called via the async path"

    elif name == "done":
        return f"DONE: {args.get('summary', '')}"

    else:
        return f"ERROR: Unknown tool: {name}"


def _tests_passed(pytest_output: str) -> bool:
    lower = pytest_output.lower()
    return "passed" in lower and "failed" not in lower and "error" not in lower


def _summarise_tool_result(tool_name: str, result: str) -> str:
    """Return a short, human-readable summary of a tool result for terminal display.

    Never dumps raw file content — only shows counts, pass/fail, and the first
    error line.  Full content is always available on disk.
    """
    if not result or not result.strip():
        return ""

    r = result.strip()

    # patch_file — show the one-liner result
    if tool_name == "patch_file":
        return r.splitlines()[0]  # "OK: patched file.py (N line(s) changed)" or ERROR

    # write_file / read_file — show char/line count, not the code
    if tool_name == "write_file":
        lines = r.splitlines()
        if r.lower().startswith("ok:"):
            return r.split("\n")[0]          # "OK: wrote N chars to path/file.py"
        return f"Written ({len(lines)} lines)"

    if tool_name == "read_file":
        lines = [l for l in r.splitlines() if l.strip()]
        if r.lower().startswith("error") or r.lower().startswith("file not found"):
            return r.splitlines()[0]
        return f"Read {len(lines)} lines"

    # run_tests — show pass/fail summary from the pytest summary line
    if tool_name == "run_tests":
        if r.startswith("TIMEOUT:"):
            return r.splitlines()[0]
        # Look for pytest summary line: "X passed, Y failed in Zs"
        for line in reversed(r.splitlines()):
            if "passed" in line or "failed" in line or "error" in line:
                return line.strip()
        return r.splitlines()[0] if r else ""

    # validate_python — short OK / error
    if tool_name == "validate_python":
        return r.splitlines()[0]

    # validate_connector_rules — show violation count or OK
    if tool_name == "validate_connector_rules":
        if "VIOLATION" in r:
            count = r.count("VIOLATION")
            return f"{count} violation(s) found — see fix prompt"
        return r.splitlines()[0]

    # run_smoke_test — first meaningful line
    if tool_name == "run_smoke_test":
        return r.splitlines()[0]

    # list_files — compact
    if tool_name == "list_files":
        files = [l.strip() for l in r.splitlines() if l.strip()]
        return f"{len(files)} file(s): {', '.join(files[:5])}{'…' if len(files) > 5 else ''}"

    # done — show summary message
    if tool_name == "done":
        return r.splitlines()[0][:120]

    # Default: first non-empty line, capped at 120 chars
    first = next((l.strip() for l in r.splitlines() if l.strip()), "")
    return first[:120] + ("…" if len(first) > 120 else "")


# ── Core agentic loop ─────────────────────────────────────────────────────────

async def _gemini_agentic_loop(
    connector_dir: Path,
    *,
    system_prompt: str,
    initial_message: str,
    tools: List[Dict],
    log_cb: LogCallback = None,
    max_iterations: int = 15,
    stop_on_done: bool = True,       # stop when model calls done()
    stop_on_tests_pass: bool = False, # stop when run_tests() output shows all passing
    require_file: Optional[str] = None,  # if set, re-prompt when Gemini returns text without writing this file
    require_file_min_size: int = 0,  # if > 0, re-prompt if written file is smaller than this (chars)
    protected_files: set = None,  # files that cannot be written (e.g. connector.py during fix_tests)
    validate_connector_on_done: bool = False,  # ONLY True for write_connector — blocks done() if violations remain
    target_methods: list[str] | None = None,  # when set, run_tests() uses -k filter for only these methods
) -> Dict[str, Any]:
    """Core Gemini tool-calling loop shared by all agentic tasks.

    Returns:
        {"success": bool, "iterations": int, "result": str, "message": str}
    """
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("INTEGRATION_GEMINI_API_KEY not set")

    model = settings.GEMINI_MODEL
    # Use streamGenerateContent so tokens arrive incrementally — enables real-time logs
    # during the 15-30 s Gemini thinking phase (batch generateContent would show nothing).
    def _make_url(m: str) -> str:
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{m}:streamGenerateContent?alt=sse&key={settings.GEMINI_API_KEY}"
        )
    url = _make_url(model)

    async def _log(level: str, msg: str) -> None:
        if log_cb:
            try:
                await log_cb(level, msg)
            except Exception:
                pass

    await _log("info", f"🤖 Gemini agentic ({model})...")

    gen_config: Dict[str, Any] = {"maxOutputTokens": 60000}
    if settings.GEMINI_THINKING_BUDGET != 0:
        gen_config["thinkingConfig"] = {"thinkingBudget": settings.GEMINI_THINKING_BUDGET}

    # Use default tool set (write_file + done) when caller passes tools=None
    _effective_tools = tools if tools is not None else _GENERATION_TOOLS

    contents: List[Dict] = [{"role": "user", "parts": [{"text": initial_message}]}]
    payload: Dict[str, Any] = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "tools": _effective_tools,
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": gen_config,
    }

    iterations = 0
    last_result = ""
    success = False
    _RETRYABLE = {429, 503}
    # Separate retry delays for rate-limit (429) vs overload (503).
    # 429 quota windows are typically 60 s — wait longer between retries.
    _RETRY_DELAYS_429 = [20, 60, 120]              # 3 retries: 20s, 60s, 120s
    _RETRY_DELAYS_503 = [5, 10, 20, 40, 60, 120]  # 6 retries — Gemini 503s are transient, just keep retrying
    # Fallback model with a separate (higher) RPM quota
    _FALLBACK_MODEL = "gemini-2.0-flash-lite"
    _active_model = model  # tracks which model is currently in use

    # No-progress early stop: track failing-test counts and collection error counts
    # from run_tests calls.  If neither improves after _STUCK_LIMIT consecutive
    # run_tests invocations, bail early.
    _last_fail_count: Optional[int] = None
    _last_error_count: Optional[int] = None
    _stuck_run_tests = 0
    _STUCK_LIMIT = 3
    _malformed_consecutive = 0   # consecutive MALFORMED_FUNCTION_CALL count

    # General stuck detection for validate_connector_rules / run_smoke_test:
    # if the same result appears _STUCK_LIMIT times in a row → Gemini is looping, bail out.
    _tool_last_result: Dict[str, str] = {}   # tool_name → last result string
    _tool_repeat_count: Dict[str, int] = {}  # tool_name → consecutive repeat count
    _STUCK_TOOLS = {"validate_connector_rules"}
    _STUCK_TOOL_LIMIT = 5

    async with httpx.AsyncClient(timeout=300.0) as client:
        while iterations < max_iterations:
            iterations += 1

            # Respect Stop button — check for cancellation at the top of every iteration
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                await _log("warn", "  🛑 Gemini loop cancelled by user (Stop).")
                raise

            # ── Call Gemini ──
            await _log("info", f"  ⏳ Gemini thinking... (iteration {iterations}/{max_iterations})")
            # ── Call Gemini with true SSE streaming (tokens arrive in real-time) ──
            # client.stream() opens the connection and yields lines immediately —
            # unlike client.post() which waits for the full response before returning.
            finish_reason = "STOP"
            parts: List[Dict] = []

            _last_status: Optional[int] = None
            _iter_success = False
            _retry_delays = _RETRY_DELAYS_503  # default; updated on first 429

            for attempt in range(1, max(len(_RETRY_DELAYS_429), len(_RETRY_DELAYS_503)) + 2):  # up to max retries + 1
                delay = 0 if attempt == 1 else _retry_delays[min(attempt - 2, len(_retry_delays) - 1)]
                if delay:
                    _status_label = "rate-limited (429)" if _last_status == 429 else "overloaded (503)"
                    await _log("warn", f"  ⏳ Gemini {_status_label} — retrying in {delay}s (attempt {attempt}, model: {_active_model})...")
                    await asyncio.sleep(delay)
                try:
                    async with client.stream(
                        "POST", url,
                        headers={"Content-Type": "application/json"},
                        json=payload,
                    ) as resp:
                        if resp.status_code in _RETRYABLE:
                            _last_status = resp.status_code
                            _retry_delays = _RETRY_DELAYS_429 if resp.status_code == 429 else _RETRY_DELAYS_503
                            _max_retries = len(_retry_delays)
                            if attempt > _max_retries:
                                # Exhausted retries on current model — try fallback model
                                if _active_model != _FALLBACK_MODEL:
                                    _active_model = _FALLBACK_MODEL
                                    url = _make_url(_active_model)
                                    await _log("warn", f"  ⚠ Rate limit exhausted on primary model — switching to {_active_model}...")
                                    # Reset attempt count for the fallback model
                                    _last_status = None
                                    _retry_delays = _RETRY_DELAYS_503
                                    # Don't raise — the outer while loop will retry with new model
                                    break
                                else:
                                    # Both models exhausted — raise to surface the error
                                    await resp.aread()
                                    resp.raise_for_status()
                            continue
                        resp.raise_for_status()

                        _stream_buf = ""
                        async for raw_line in resp.aiter_lines():
                            raw_line = raw_line.strip()
                            if not raw_line or raw_line == "data: [DONE]":
                                continue
                            if raw_line.startswith("data:"):
                                raw_line = raw_line[5:].strip()
                            try:
                                chunk = json.loads(raw_line)
                            except json.JSONDecodeError:
                                _stream_buf += raw_line
                                try:
                                    chunk = json.loads(_stream_buf)
                                    _stream_buf = ""
                                except json.JSONDecodeError:
                                    continue
                            c = (chunk.get("candidates") or [{}])[0]
                            _chunk_reason = c.get("finishReason", "")
                            if _chunk_reason:
                                finish_reason = _chunk_reason
                            for p in c.get("content", {}).get("parts") or []:
                                if "text" in p:
                                    # Do NOT stream raw text/code lines — they flood the terminal
                                    # with Python code content and obscure the real status.
                                    # Only structural events (tool calls, results, errors) are logged.
                                    # Merge consecutive same-type text parts
                                    if parts and "text" in parts[-1] and parts[-1].get("thought") == p.get("thought"):
                                        parts[-1]["text"] += p["text"]
                                    else:
                                        parts.append(dict(p))
                                elif "functionCall" in p:
                                    parts.append(dict(p))
                    _iter_success = True
                    break  # stream completed successfully — exit retry loop
                except httpx.TimeoutException:
                    if attempt > len(_retry_delays):
                        raise
                    continue

            # If fallback model was triggered (broke out of retry loop without success),
            # retry this same iteration with the new model (url already updated above).
            if not _iter_success:
                iterations -= 1  # don't count the failed iteration
                continue

            contents.append({"role": "model", "parts": parts})

            # Separate thought parts (Gemini internal reasoning) from regular text parts
            thought_parts = [p["text"] for p in parts if "text" in p and p.get("thought") and p["text"].strip()]
            text_parts = [p["text"] for p in parts if "text" in p and not p.get("thought") and p["text"].strip()]
            function_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if not function_calls:
                if finish_reason == "MAX_TOKENS":
                    # Response was cut off — send a continuation prompt instead of stopping.
                    # This lets Gemini resume (e.g. finish writing a large test file).
                    await _log("warn", f"  ⚠ Response truncated (MAX_TOKENS) at iteration {iterations} — asking Gemini to continue...")
                    contents.append({"role": "user", "parts": [{"text": "Your response was cut off due to token limits. Please continue from where you left off. If you were writing a file, call write_file with the complete content. Then call done(summary) when finished."}]})
                    payload["contents"] = contents
                    continue
                if finish_reason == "MALFORMED_FUNCTION_CALL":
                    # Gemini tried to call a tool but the JSON was malformed (e.g. write_file with
                    # a very large file that got truncated/corrupted in transit).
                    _malformed_consecutive += 1
                    await _log("warn", f"  ⚠ MALFORMED_FUNCTION_CALL at iteration {iterations} (consecutive: {_malformed_consecutive}) — asking Gemini to retry...")
                    if _malformed_consecutive >= 3:
                        await _log("warn", "  ⏹ Too many consecutive malformed calls — stopping fix loop to prevent infinite retry")
                        break
                    elif _malformed_consecutive == 2:
                        # Second failure: likely trying to rewrite a huge file. Force targeted edits.
                        contents.append({"role": "user", "parts": [{"text": (
                            "Your tool call was malformed again — the file content is too large to send at once. "
                            "DO NOT rewrite the entire file. Instead:\n"
                            "1. Read the file first to find the exact lines that need changing.\n"
                            "2. Write ONLY the corrected version of the specific function(s) that need fixing.\n"
                            "3. Keep the rest of the file unchanged — write only the minimum diff needed.\n"
                            "Make your write_file content as short as possible while still fixing the bug."
                        )}]})
                    else:
                        contents.append({"role": "user", "parts": [{"text": (
                            "Your last tool call was malformed (the JSON was invalid or truncated). "
                            "Please retry with a shorter write_file content — only write the specific "
                            "functions or lines that need changing, not the entire file. "
                            "Then call done(summary) when finished."
                        )}]})
                    payload["contents"] = contents
                    continue
                # If a required file hasn't been written yet, or was written with
                # only a short summary sentence instead of real content, re-prompt.
                if require_file:
                    _req_path = connector_dir / require_file
                    _req_exists = _req_path.exists()
                    _req_size = _req_path.stat().st_size if _req_exists else 0
                    _min_size = max(300, require_file_min_size)  # caller can raise the bar
                    if not _req_exists or _req_size < _min_size:
                        _reason = "did not call write_file" if not _req_exists else f"wrote only {_req_size} chars (too short — summary instead of full content)"
                        await _log("warn", f"  ⚠ Gemini {_reason} — re-prompting to write {require_file}...")
                        contents.append({"role": "user", "parts": [{"text": (
                            f"The file '{require_file}' is {'missing' if not _req_exists else 'too short (' + str(_req_size) + ' chars)'}. "
                            f"You MUST call write_file('{require_file}', content) with the FULL, COMPLETE content — "
                            f"not a summary sentence. Write every section in detail, then call done(summary) when finished."
                        )}]})
                        payload["contents"] = contents
                        continue
                # Model finished without calling done() — treat as success
                success = True
                last_result = " ".join(text_parts)
                break

            # ── Execute tool calls ──
            if finish_reason == "MAX_TOKENS":
                await _log("warn", f"  ⚠ MAX_TOKENS hit at iteration {iterations} — executing {len(function_calls)} tool call(s) and continuing...")
            function_responses = []
            _done_blocked = False  # set True when done() is rejected due to rule violations
            _malformed_consecutive = 0  # valid tool call received — reset malformed counter
            for fc in function_calls:
                tool_name = fc["name"]
                tool_args = fc.get("args", {})

                args_preview = ", ".join(
                    f"{k}={repr(v)[:80]}" for k, v in tool_args.items()
                    if k != "content"  # skip file content — logged separately by write_file handler
                )
                await _log("info", f"  🔧 {tool_name}({args_preview})")

                # search_knowledge is async — handle separately
                if tool_name == "search_knowledge" and _active_knowledge_fn is not None:
                    try:
                        result_str = await _active_knowledge_fn(tool_args.get("query", ""))
                        result_str = result_str or "No relevant results found."
                    except Exception as _ke:
                        result_str = f"Knowledge search error: {_ke}"
                elif tool_name == "search_knowledge":
                    result_str = "Knowledge base not available for this task."
                else:
                    try:
                        result_str = await asyncio.get_running_loop().run_in_executor(
                            None, _execute_tool, tool_name, tool_args, connector_dir, protected_files, target_methods
                        )
                    except asyncio.CancelledError:
                        await _log("warn", "  🛑 Tool execution cancelled by user (Stop).")
                        raise
                    except Exception as _tool_exc:
                        result_str = f"ERROR: tool execution failed — {_tool_exc}"

                # Log a concise summary of the tool result — never dump raw file content.
                # Full content is available in the files on disk; the terminal is for status.
                _result_summary = _summarise_tool_result(tool_name, result_str)
                if _result_summary:
                    await _log("info", f"     {_result_summary}")

                # Check stop conditions
                if stop_on_done and tool_name == "done":
                    # ── Guard: auto-run validate_connector_rules before accepting done() ──
                    # ONLY active for write_connector (validate_connector_on_done=True).
                    # Must NOT run for generate_test_guidelines, write_tests, fix loops, etc.
                    # — those steps can't fix connector.py violations and would get stuck forever.
                    if validate_connector_on_done and connector_dir is not None:
                        _conn_path = connector_dir / "connector.py"
                        if _conn_path.exists():
                            _rules_result = _execute_tool("validate_connector_rules", {}, connector_dir)
                            _has_violations = (
                                "VIOLATION" in _rules_result
                                or "VIOLATIONS FOUND" in _rules_result
                            )
                            if _has_violations:
                                await _log("warn",
                                    f"  ⚠️ done() blocked — validate_connector_rules found violations. "
                                    f"Fix them before calling done().")
                                # Inject violation result as the done() response so Gemini sees it
                                _done_reject = (
                                    f"BLOCKED: done() cannot be called while violations remain.\n\n"
                                    f"{_rules_result}\n\n"
                                    f"Fix ALL violations above, then call done()."
                                )
                                function_responses.append({
                                    "functionResponse": {"name": tool_name, "response": {"result": _done_reject}}
                                })
                                _done_blocked = True
                                break  # exit the for loop (for-else won't run; handled below)

                    success = True
                    last_result = tool_args.get("summary", "Done")
                    function_responses.append({
                        "functionResponse": {"name": tool_name, "response": {"result": result_str}}
                    })
                    contents.append({"role": "user", "parts": function_responses})
                    payload["contents"] = contents
                    break

                if stop_on_tests_pass and tool_name == "run_tests":
                    last_result = result_str
                    if _tests_passed(result_str):
                        success = True
                        await _log("success", "  ✅ All tests passing!")
                    else:
                        # No-progress detection: count failing tests AND collection errors
                        import re as _re_np
                        _fail_match = _re_np.search(r"(\d+) failed", result_str)
                        _err_match = _re_np.search(r"(\d+) error", result_str)
                        _cur_fail = int(_fail_match.group(1)) if _fail_match else None
                        _cur_err = int(_err_match.group(1)) if _err_match else 0

                        # Detect timeout — counts toward stuck immediately
                        if result_str.startswith("TIMEOUT:"):
                            _stuck_run_tests += 1
                            if _stuck_run_tests >= _STUCK_LIMIT:
                                await _log("warn",
                                    f"  ⏹ Tests keep timing out after {_STUCK_LIMIT} attempts "
                                    "— stopping fix loop (reduce test count or increase timeout)")
                                break

                        # Detect stuck on collection errors (IndentationError, ImportError, etc.)
                        # when pytest reports "0 failed, N errors" the fail count is None/0
                        # so we track error count separately.
                        elif _cur_err > 0:
                            if _last_error_count is not None and _cur_err >= _last_error_count:
                                _stuck_run_tests += 1
                                if _stuck_run_tests >= _STUCK_LIMIT:
                                    await _log("warn", f"  ⏹ No progress on collection errors after {_STUCK_LIMIT} iterations ({_cur_err} still erroring) — stopping")
                                    break
                            else:
                                _stuck_run_tests = 0  # improvement — reset counter
                            _last_error_count = _cur_err
                        elif _cur_fail is not None:
                            if _last_fail_count is not None and _cur_fail >= _last_fail_count:
                                _stuck_run_tests += 1
                                if _stuck_run_tests >= _STUCK_LIMIT:
                                    await _log("warn", f"  ⏹ No improvement after {_STUCK_LIMIT} run_tests calls ({_cur_fail} still failing) — stopping fix loop early")
                                    break
                            else:
                                _stuck_run_tests = 0  # improvement — reset counter
                            _last_fail_count = _cur_fail

                # ── General stuck detection for validate_connector_rules / run_smoke_test ──
                # Only count toward stuck when connector.py EXISTS and the result is a real
                # failure (not a "file not found" error from calling too early).
                # This prevents false positives when Gemini calls these tools before writing files.
                if tool_name in _STUCK_TOOLS:
                    # Normalize: strip timing noise (e.g. "0.23s", "duration: 1.4s") and
                    # line numbers from tracebacks so slight variation doesn't defeat detection.
                    import re as _stuck_re
                    _cur_norm = _stuck_re.sub(r'\d+\.\d+s', 'Xs', result_str.strip())
                    _cur_norm = _stuck_re.sub(r'line \d+', 'line N', _cur_norm)
                    # Skip ERROR: and "not found" — prerequisite errors (file not yet written).
                    _is_prereq_error = (
                        _cur_norm.startswith("ERROR:")
                        or "not found" in _cur_norm.lower()
                    )
                    if not _is_prereq_error and _cur_norm:
                        _prev = _tool_last_result.get(tool_name, "")
                        if _cur_norm == _prev:
                            _tool_repeat_count[tool_name] = _tool_repeat_count.get(tool_name, 0) + 1
                            if _tool_repeat_count[tool_name] >= _STUCK_TOOL_LIMIT:
                                await _log("warn",
                                    f"  ⏹ {tool_name} returned identical result {_STUCK_TOOL_LIMIT} times in a row — "
                                    "Gemini is stuck in a loop. Stopping early to avoid wasting iterations.")
                                success = False
                                break
                        else:
                            _tool_repeat_count[tool_name] = 0  # progress made — reset
                        _tool_last_result[tool_name] = _cur_norm

                function_responses.append({
                    "functionResponse": {"name": tool_name, "response": {"result": result_str}}
                })

            else:
                # Only continue loop if done() was not called
                contents.append({"role": "user", "parts": function_responses})
                payload["contents"] = contents
                if success:
                    break
                continue

            # For loop exited via break — either done() was accepted, done() was blocked, or stuck
            if _done_blocked:
                # done() was rejected — send the violation feedback and give Gemini another iteration
                contents.append({"role": "user", "parts": function_responses})
                payload["contents"] = contents
                continue  # outer while loop

            # done() was accepted — exit outer loop
            break

    logger.info("agentic.done", success=success, iterations=iterations, task=system_prompt[:40])
    return {"success": success, "iterations": iterations, "result": last_result, "message": last_result}


# ── Public smoke test runner — called by the dedicated smoke_test step ────────

async def run_connector_smoke_test(connector_dir: Path) -> str:
    """Run the connector smoke test in a subprocess and return the result string.

    This is a standalone function (not part of the Gemini agentic loop) so it can
    be called directly from the smoke_test step handler after all files are generated.
    """
    import sys as _sys, os as _os, subprocess as _sp, tempfile as _tf, textwrap as _tw, json as _json

    conn_path = connector_dir / "connector.py"
    if not conn_path.exists():
        return "ERROR: connector.py not found — run write_connector first"

    # Build smoke config dynamically from connector metadata so we don't hardcode connector-specific keys
    _smoke_config: dict = {}
    _meta_path = connector_dir / "metadata" / "connector.json"
    if _meta_path.exists():
        try:
            _meta = _json.loads(_meta_path.read_text(encoding="utf-8"))
            for _field in _meta.get("install_fields", []):
                _key = _field.get("key", "")
                if not _key:
                    continue
                # Special handling for JSON-typed fields
                if _field.get("type") == "json" or "json" in _key.lower():
                    _smoke_config[_key] = '{"type":"service_account"}'
                else:
                    _smoke_config[_key] = "test"
        except Exception:
            pass
    # Fallback to a broad set of common keys if metadata is missing
    if not _smoke_config:
        _smoke_config = {
            "api_key": "test", "client_id": "test", "client_secret": "test",
            "username": "test", "password": "test",
            "service_account_json": '{"type":"service_account"}',
        }
    _smoke_config_repr = repr(_smoke_config)

    smoke_script = _tw.dedent("""
    import sys, os, asyncio, traceback
    from unittest.mock import AsyncMock, MagicMock, patch

    import structlog
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))

    import httpx
    _mock_response = MagicMock()
    _mock_response.status_code = 200
    _mock_response.json.return_value = {"resultInfo": {"resultStatus": "S"}, "status": "SUCCESS"}
    _mock_response.raise_for_status = MagicMock()
    _mock_client_instance = AsyncMock()
    _mock_client_instance.request = AsyncMock(return_value=_mock_response)
    _mock_client_instance.post = AsyncMock(return_value=_mock_response)
    _mock_client_instance.get = AsyncMock(return_value=_mock_response)
    _mock_client_instance.__aenter__ = AsyncMock(return_value=_mock_client_instance)
    _mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    results = []

    with patch('httpx.AsyncClient', return_value=_mock_client_instance):
        try:
            import importlib as _il, os as _os3
            _pkg_name = _os3.path.basename(_os3.getcwd())
            try:
                _conn_mod = _il.import_module(f"{_pkg_name}.connector")
            except Exception:
                import connector as _conn_mod
            from shared.base_connector import BaseConnector
            cls = next(
                (v for v in vars(_conn_mod).values()
                 if isinstance(v, type) and issubclass(v, BaseConnector) and v is not BaseConnector),
                None
            )
            if not cls:
                print("FAIL: no BaseConnector subclass found in connector.py")
                sys.exit(1)
            results.append(f"PASS: class {cls.__name__} found")
        except Exception as e:
            print(f"FAIL: import error — {traceback.format_exc()}")
            sys.exit(1)

        try:
            inst = cls(
                tenant_id="smoke-tenant",
                connector_id="smoke-connector",
                config=__SMOKE_CONFIG__,
            )
            results.append(f"PASS: {cls.__name__}() instantiated")
        except Exception as e:
            print(f"FAIL: instantiation — {traceback.format_exc()}")
            sys.exit(1)

        try:
            async def _run_install():
                with patch.object(cls, 'save_config', AsyncMock(return_value=None)), \\
                     patch.object(cls, 'get_token', AsyncMock(return_value=None)), \\
                     patch.object(cls, 'set_token', AsyncMock(return_value=None)):
                    return await inst.install()
            result = asyncio.run(_run_install())
            if not hasattr(result, 'health'):
                print(f"FAIL: install() returned {type(result)} — expected ConnectorStatus with .health field")
                sys.exit(1)
            if not hasattr(result, 'connector_id') or not result.connector_id:
                print(f"FAIL: install() ConnectorStatus missing connector_id — add connector_id=self.connector_id")
                sys.exit(1)
            results.append(f"PASS: install() returned ConnectorStatus(health={result.health}, auth_status={result.auth_status})")
        except TypeError as e:
            if "unexpected keyword argument" in str(e):
                print(f"FAIL: install() logger error — {e}\\nFix: use structlog not logging.getLogger")
            else:
                print(f"FAIL: install() TypeError — {traceback.format_exc()}")
            sys.exit(1)
        except Exception as e:
            print(f"FAIL: install() — {traceback.format_exc()}")
            sys.exit(1)

    for r in results:
        print(r)
    print("SMOKE TEST PASSED")
    """).replace("__SMOKE_CONFIG__", _smoke_config_repr)

    try:
        with _tf.NamedTemporaryFile(mode='w', suffix='.py', delete=False, dir=str(connector_dir)) as f:
            f.write(smoke_script)
            tmp_path = f.name

        env = {**_os.environ}
        existing_pp = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = (
            f"{str(connector_dir.parent)}{_os.pathsep}"
            f"{str(connector_dir)}{_os.pathsep}"
            f"{str(_CONNECTORS_ROOT)}{_os.pathsep}{existing_pp}"
        )

        proc = _sp.run(
            [_sys.executable, tmp_path],
            capture_output=True, text=True, timeout=30, env=env,
            cwd=str(connector_dir),
        )
        _os.unlink(tmp_path)

        output = (proc.stdout + proc.stderr).strip()
        if proc.returncode == 0 and "SMOKE TEST PASSED" in output:
            lines = [l for l in output.splitlines() if l.strip()]
            return "SMOKE TEST PASSED:\n" + "\n".join(f"  ✓ {l}" for l in lines if l != "SMOKE TEST PASSED")
        else:
            return f"SMOKE TEST FAILED:\n{output}"
    except _sp.TimeoutExpired:
        return "SMOKE TEST FAILED: timed out (30s) — install() is likely making real network calls. Check install() only validates config keys."
    except Exception as e:
        return f"SMOKE TEST ERROR: {e}"


# ── Public API ────────────────────────────────────────────────────────────────

async def gemini_agentic_generate_connector(
    connector_dir: Path,
    *,
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate connector.py using Gemini + tool calls.

    Gemini reads shared/base_connector.py directly, writes connector.py, validates syntax.
    """
    provider = context.get("provider", "unknown")
    service_name = context.get("service_name", "Unknown")
    auth_type = context.get("auth_type", "unknown")
    sdk_package = context.get("sdk_package", "")
    user_prompt = context.get("user_prompt", "Build a standard connector.")
    plan_constraints = context.get("plan_constraints", "")
    default_config_md = context.get("default_config_md", "")

    # Derive the canonical CONNECTOR_TYPE value from the service_slug (strip _connector suffix)
    import re as _re_slug
    _raw_slug = context.get("service_slug", service_name.lower().replace(" ", "_"))
    connector_type_value = _re_slug.sub(r'_connector$', '', _raw_slug) if _raw_slug.endswith('_connector') else _raw_slug

    initial_message = (
        f"Generate a connector.py for: **{service_name}** ({provider})\n\n"
        f"- Auth type: {auth_type}\n"
        f"- SDK package: {sdk_package or 'use httpx'}\n"
        f"- **CONNECTOR_TYPE = \"{connector_type_value}\"** ← use this EXACT value, no suffix, no prefix\n"
        f"- User requirements: {user_prompt}\n"
        + (f"- Default configuration binding (MUST follow exactly):\n{default_config_md}\n" if default_config_md else "")
        + f"- Plan constraints:\n{plan_constraints}\n\n"
        f"Follow the workflow in your system prompt:\n"
        f"1. Call `list_files()` to see the pre-created package structure (config.py, helpers/, client/ are already there)\n"
        f"2. Read `{_CONNECTORS_ROOT}/shared/base_connector.py` for the exact interface\n"
        f"3. Read `config.py` to see the auth boilerplate\n"
        f"4. Write `connector.py` (and optionally client/http_client.py, helpers/, exceptions.py)\n"
        f"5. Validate every file, then call `done()`"
    )

    return await _gemini_agentic_loop(
        connector_dir,
        system_prompt=await _r2_service.get_step_prompt(
            "CONNECTOR_GEN_SYSTEM",
            _CONNECTOR_GEN_SYSTEM,
            auth_type=auth_type if auth_type and auth_type != "unknown" else None,
        ),
        initial_message=initial_message,
        tools=_GENERATION_TOOLS,
        log_cb=log_cb,
        max_iterations=25,  # budget: write(5) + validate(1) + rules(1) + fix(3) + done(1) = 11 typical; 25 gives headroom for complex connectors with multiple violation fix rounds
        stop_on_done=True,
        validate_connector_on_done=True,  # block done() if connector.py still has violations
        require_file="connector.py",
        require_file_min_size=1000,  # connector.py must be at least 1000 chars — a stub or empty file is a failure
    )


async def gemini_agentic_generate_metadata(
    connector_dir: Path,
    *,
    version: str = "1.0.0",
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate metadata/connector.json using Gemini + tool calls.

    Gemini reads connector.py directly, writes connector.json, validates JSON.
    """
    initial_message = (
        f"Generate `metadata/connector.json` for the connector in this package (version={version}).\n\n"
        "Read `connector.py` first to understand the CONNECTOR_TYPE, install() config keys, "
        "and all public methods. Then write the metadata JSON."
    )

    return await _gemini_agentic_loop(
        connector_dir,
        system_prompt=await _r2_service.get_step_prompt("METADATA_GEN_SYSTEM", _METADATA_GEN_SYSTEM),
        initial_message=initial_message,
        tools=_GENERATION_TOOLS,
        log_cb=log_cb,
        max_iterations=6,
        stop_on_done=True,
        require_file="metadata/connector.json",
    )


async def gemini_agentic_generate_instructions(
    connector_dir: Path,
    *,
    context: Dict[str, Any] = None,
    guidelines: str = "",
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate instructions/setup.md using Gemini + tool calls.

    Gemini reads connector.py and metadata/connector.json, then writes a
    step-by-step credential setup guide for the deployment form.
    The `guidelines` parameter is the INSTRUCTION_SETUP_GUIDELINES fetched from R2
    which defines the required structure and content standards.
    """
    from integration.services.step_executor import _SETUP_INSTRUCTIONS_SYSTEM
    ctx = context or {}
    provider = ctx.get("provider", "unknown")
    service_name = ctx.get("service_name", ctx.get("service", "unknown"))
    auth_type = ctx.get("auth_type", "unknown")

    # Build system prompt: base system + R2 guidelines injected as context
    base_system = await _r2_service.get_step_prompt("SETUP_INSTRUCTIONS_SYSTEM", _SETUP_INSTRUCTIONS_SYSTEM)
    if guidelines:
        system_prompt = (
            base_system
            + "\n\n---\n\n## INSTRUCTION_SETUP_GUIDELINES (from R2)\n"
            + guidelines
        )
    else:
        system_prompt = base_system

    initial_message = (
        f"Generate `instructions/setup.md` for the **{service_name}** connector "
        f"(provider: **{provider}**, auth_type: **{auth_type}**).\n\n"
        "Follow the INSTRUCTION_SETUP_GUIDELINES structure exactly.\n\n"
        "Steps:\n"
        "1. Read `connector.py` — identify every `self.config.get(key)` call\n"
        "2. Read `metadata/connector.json` — use `install_fields` for labels, help, types\n"
        f"3. Write `instructions/setup.md` — be SPECIFIC about **{provider}**'s actual portal URLs and navigation paths\n"
        "4. Call `done('Instructions written')` when finished"
    )

    return await _gemini_agentic_loop(
        connector_dir,
        system_prompt=system_prompt,
        initial_message=initial_message,
        tools=_GENERATION_TOOLS,
        log_cb=log_cb,
        max_iterations=6,
        stop_on_done=True,
        require_file="instructions/setup.md",
    )


async def gemini_agentic_generate_test_guidelines(
    connector_dir: Path,
    *,
    context: Dict[str, Any] = None,
    system_prompt: str = "",
    user_message: str = "",
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate test_guidelines.md using Gemini + tool calls.
    Gemini reads all package files and writes a connector-specific test guide.
    """
    return await _gemini_agentic_loop(
        connector_dir,
        system_prompt=system_prompt,
        initial_message=user_message,
        tools=_GENERATION_TOOLS,
        log_cb=log_cb,
        max_iterations=30,  # read(5-6) + write(1) + validate(1) + done(1) = 8 typical; 30 gives headroom for complex connectors
        stop_on_done=True,
        require_file="test_guidelines.md",
        require_file_min_size=800,
    )


async def gemini_agentic_generate_docs(
    connector_dir: Path,
    *,
    guidelines: str = "",
    extra_prompt: str = "",
    connector_name: str = "",
    provider: str = "",
    service_name: str = "",
    auth_type: str = "",
    user_prompt: str = "",
    knowledge_fn: KnowledgeQueryFn = None,
    rag_context: str = "",
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate docs/connector_docs.json using Gemini + tool calls.

    Gemini can:
    - read_file: read connector.py, metadata/connector.json, etc.
    - search_knowledge: query uploaded SDK docs / API references via vector search
    - write_file: write docs/connector_docs.json
    - validate_json / done: verify and finish
    """
    global _active_knowledge_fn
    _active_knowledge_fn = knowledge_fn  # wire for this request

    # Build system prompt — loaded from R2 shielva-integration-plans/STEP_PROMPTS/DOCS_GEN_SYSTEM.txt
    # Real quality comes from Gemini reading the actual code + querying the knowledge base.
    system = await _r2_service.get_step_prompt("DOCS_GEN_SYSTEM", _DOCS_GEN_SYSTEM)
    # Inject pre-fetched RAG context as a starting point so Gemini can deep-dive further
    if rag_context:
        system += (
            f"\n\n## Pre-fetched knowledge context (use as starting point — call search_knowledge for more)\n"
            f"{rag_context[:4000]}"  # cap to avoid blowing system prompt budget
        )

    context_block = "\n".join(filter(None, [
        f"- Connector name: {connector_name}" if connector_name else "",
        f"- Provider: {provider}" if provider else "",
        f"- Service: {service_name}" if service_name else "",
        f"- Auth type: {auth_type}" if auth_type else "",
        f"- Original user requirements: {user_prompt}" if user_prompt else "",
        f"- Extra documentation instructions: {extra_prompt}" if extra_prompt else "",
    ]))

    initial_message = (
        f"Generate production-quality documentation for the connector in: {connector_dir.name}/\n\n"
        f"## Session context\n{context_block}\n\n"
        "## Required steps (do all of these before writing):\n"
        "1. read_file('connector.py') — extract every method, config field, auth flow, error handling\n"
        "2. read_file('metadata/connector.json') if it exists — install fields, API endpoints\n"
        "3. read_file('requirements.txt') if it exists — SDK dependencies\n"
        "4. search_knowledge with specific queries about this API:\n"
        f"   - '{service_name} API authentication flow'\n"
        f"   - '{service_name} API rate limits'\n"
        f"   - '{service_name} API error codes'\n"
        f"   - '{service_name} SDK methods examples'\n"
        "   (call search_knowledge multiple times with different queries)\n"
        "5. write_file('docs/connector_docs.json') with deeply connector-specific content\n"
        "   - Use REAL method names, REAL config fields, REAL API details from your research\n"
        "   - NEVER write placeholder text\n"
        "6. validate_json('docs/connector_docs.json')\n"
        "7. done(summary)\n\n"
        "Quality check before done(): Is every section specific to THIS connector? "
        "Could a reader set it up and use it from this doc alone?"
    )

    return await _gemini_agentic_loop(
        connector_dir,
        system_prompt=system,
        initial_message=initial_message,
        tools=_GENERATION_TOOLS,
        log_cb=log_cb,
        max_iterations=20,   # more iterations → deeper knowledge search
        stop_on_done=True,
    )


_TEST_GEN_SYSTEM = """You are a Python testing expert generating pytest unit tests for a Shielva connector.

## Workflow
1. Read `connector.py` — understand the class name, method signatures, which client object is used (e.g. `self.gmail_client`), and every `await self.<client>.<method>(...)` call
2. Read every file in `client/` — check which methods are `async def`. Every awaited method MUST be mocked as `AsyncMock`. This is how you know — not guessing.
3. Read `helpers/` files if the connector imports from helpers/
4. Write `tests/test_connector.py` with complete test cases for the requested methods
5. Call `validate_python('tests/test_connector.py')` — fix any syntax errors found
6. Call `run_tests` — iterate until ALL tests pass or you are satisfied
7. Call `done(summary)` when finished

## MANDATORY imports at top of file (copy exactly):
```python
import pytest
import httpx
from unittest.mock import patch, MagicMock, AsyncMock
from googleapiclient.errors import HttpError
from connector import <ClassName>
from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, AuthStatus,
    TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
```

## Structure
- One test class per method: class TestMethodName
- At least 2 tests per method: success path + error/edge case
- Use @pytest.mark.asyncio on every async test function
- asyncio_mode=auto is set in pytest.ini — decorators are optional but add them anyway

## Constructor
ClassName(tenant_id='test-tenant', connector_id='test-connector')
NEVER pass token_info, credentials, or config to __init__

## ⚠ CRITICAL — mock ALL BaseConnector DB/network methods in every test that calls install/authorize/sync:
@patch.object(ClassName, 'set_token', new_callable=AsyncMock)
@patch.object(ClassName, 'get_token', new_callable=AsyncMock, return_value=None)
@patch.object(ClassName, 'ingest_batch', new_callable=AsyncMock)
Without these mocks the tests will hang forever connecting to a real database.

## ConnectorStatus fields: connector_id(str), health(ConnectorHealth), auth_status(AuthStatus), connector_type(str)
## NO .status field — use .health and .auth_status
## ConnectorHealth: HEALTHY, DEGRADED, OFFLINE, UNHEALTHY
## AuthStatus: PENDING, CONNECTED, EXPIRED, FAILED, MISSING_CREDENTIALS, TOKEN_EXPIRED, AUTHENTICATED, INVALID_CREDENTIALS
## AuthStatus.UNAUTHENTICATED does NOT exist
## SyncStatus: IDLE, SYNCING, COMPLETED, FAILED, SUCCESS, PARTIAL

## ⚠ CRITICAL — Mock patch path rule (most common mistake)
ALWAYS patch where the name is USED, NOT where it is defined.
connector.py does `from client.gmail_client import GmailClient`
→ GmailClient now lives in the `connector` module namespace
→ patch as `'connector.GmailClient'`   ✅ CORRECT
→ NEVER patch `'client.gmail_client.GmailClient'`  ❌ WRONG — connector.py never sees it

## ⚠ CRITICAL — External API client mocking (AsyncMock required)
When the connector creates an API client object (e.g. GmailAPIClient, SlackClient, etc.),
patch the CLASS so it returns a MagicMock instance, then set EACH awaited method as AsyncMock:

```python
@patch('connector.GmailAPIClient')   # ← always 'connector.<ClassName>', never the source module
async def test_something(self, mock_client_class, ...):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    # ✅ CORRECT — every method the connector awaits must be AsyncMock
    mock_client.get_profile = AsyncMock(return_value={"emailAddress": "user@example.com"})
    mock_client.list_messages = AsyncMock(side_effect=[{"messages": [{"id": "1"}]}])
    mock_client.get_message = AsyncMock(return_value={"id": "1", "raw": "..."})

    # ❌ WRONG — this creates a sync attribute → TypeError: 'dict' object can't be awaited
    # mock_client.get_profile.return_value = {"emailAddress": "user@example.com"}
```

Rule: Read `connector.py` first. For every line with `await self.<client>.<method>(...)`,
that method on the mock MUST be set as `mock_client.<method> = AsyncMock(...)`.

## Patching rules
- Use patch.object(ClassName, 'method_name') for connector methods
- External client class: patch('connector.ClientClassName') — patch where it is USED, not where defined
- Module-level helper functions (e.g. verify_checksum_helper, generate_checksum): patch at module level
  ✅ patch('connector.verify_checksum_helper')   ← helper is imported INTO connector.py
  ❌ patch('connector.helpers.verify_checksum_helper')  ← connector module has no .helpers attribute
  ❌ patch('helpers.utils.verify_checksum_helper')  ← wrong, patches source not caller
- NEVER add a mock parameter without a matching @patch.object decorator
- NEVER use freezegun, factory_boy, hypothesis, faker (not installed)
- ONLY valid shared module: shared.base_connector
  shared.exceptions / shared.models / shared.utils do NOT exist

## ⚠ CRITICAL — Connector config attributes in assertions
Connectors store config values as direct attributes in __init__:
  self.merchant_key = self.config.get("merchant_key", "")
  self.api_key = self.config.get("api_key", "")
  self.client_id = self.config.get("client_id", "")

These live on the CONNECTOR INSTANCE, not on self.client (which is a Mock).

❌ WRONG: connector.client.merchant_key  ← client is a MagicMock; no merchant_key attribute
✅ CORRECT options (pick one):
  a) connector.merchant_key              ← actual attribute on the connector
  b) "test_merchant_key"                 ← the literal value you set in the fixture config

When asserting a helper was called with a connector attribute:
  # ✅ Use the fixture config value directly:
  mock_verify_checksum.assert_called_once_with(data_string, "test_merchant_key", checksum)
  # ✅ OR use the connector attribute:
  mock_verify_checksum.assert_called_once_with(data_string, connector.merchant_key, checksum)
  # ❌ NEVER:
  mock_verify_checksum.assert_called_once_with(data_string, connector.client.merchant_key, checksum)

## ⚠ MANDATORY imports — include ALL of these you use in the test file:
```python
import json          # REQUIRED if you use json.dumps() anywhere in the test
import pytest
import httpx
from unittest.mock import patch, MagicMock, AsyncMock
from connector import <ClassName>
from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, AuthStatus,
    TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
```
NEVER use json.dumps without `import json` at the top of the file.

## HttpError mocking
mock_resp = MagicMock(); mock_resp.status = 401; mock_resp.reason = 'Unauthorized'
raise HttpError(resp=mock_resp, content=b'error')

## Output: valid Python only — no markdown fences, no prose"""


async def _ingest_connector_files(
    connector_dir: Path,
    tenant_id: str,
    provider: str,
    service: str,
    log_cb: LogCallback = None,
) -> None:
    """Ingest all generated connector .py files into the RAG KB before the test/fix loop.

    This ensures search_knowledge("GmailAPIClient async methods") returns real results —
    Gemini doesn't have to guess which methods are async.
    Skips tests/ and __pycache__. Idempotent — same doc_id = upsert, not duplicate.
    """
    try:
        from integration.services import knowledge_service
        py_files = sorted(
            f for f in connector_dir.rglob("*.py")
            if "__pycache__" not in f.parts and "tests" not in f.parts
        )
        for fpath in py_files:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                continue
            rel = str(fpath.relative_to(connector_dir))
            await knowledge_service.ingest_step_output(
                content=content,
                filename=rel,
                tenant_id=tenant_id,
                provider=provider,
                service=service,
                step_type="connector_code",
            )
        if log_cb:
            await log_cb("info", f"📚 Ingested {len(py_files)} connector files into KB for search_knowledge")
    except Exception as exc:
        logger.warning("agentic.ingest_connector_files_failed", error=str(exc))


async def gemini_agentic_generate_tests(
    connector_dir: Path,
    *,
    methods: List[str],
    class_name: str,
    knowledge_fn: KnowledgeQueryFn = None,
    tenant_id: str = "",
    provider: str = "",
    service: str = "",
    test_guidelines: str = "",
    existing_tests: str = "",
    reset: bool = False,
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate AND verify pytest tests using the Gemini tool-calling loop.

    Gemini reads connector.py + client/ files (via search_knowledge or read_file),
    writes tests/test_connector.py, runs pytest, fixes failures, iterates until passing.
    Each write_file call triggers deterministic autoflake+ruff post-fix automatically.
    """
    global _active_knowledge_fn
    _active_knowledge_fn = knowledge_fn

    # Ingest all connector files into KB so search_knowledge returns real method signatures
    if knowledge_fn and tenant_id and provider and service:
        await _ingest_connector_files(connector_dir, tenant_id, provider, service, log_cb)

    # Extract ground truth from connector AST — prevents Gemini from hallucinating class/exception names
    from integration.services.step_executor import _extract_connector_ground_truth as _egt
    _agentic_ground_truth = _egt(connector_dir)
    _agentic_ground_truth_block = ""
    if _agentic_ground_truth:
        _agentic_ground_truth_block = (
            "## ══════════════════════════════════════════════════════════\n"
            "## GROUND TRUTH — EXACT NAMES FROM CONNECTOR SOURCE (DO NOT DEVIATE)\n"
            "## ══════════════════════════════════════════════════════════\n"
            + _agentic_ground_truth
            + "\n## ══════════════ END OF GROUND TRUTH ══════════════\n\n"
        )

    # Build system prompt — inject connector-specific guidelines if available
    _base_test_gen = await _r2_service.get_step_prompt("TEST_GEN_SYSTEM", _TEST_GEN_SYSTEM)
    system_prompt = _base_test_gen
    if test_guidelines and len(test_guidelines.strip()) > 300:
        system_prompt = (
            "## ══════════════════════════════════════════════════════════\n"
            "## CONNECTOR-SPECIFIC TEST GUIDELINES — READ THIS FIRST\n"
            "## These guidelines contain exact fixture blueprints, mock patterns,\n"
            "## per-method test specs, and required assertions for THIS connector.\n"
            "## ══════════════════════════════════════════════════════════\n"
            + test_guidelines.strip()
            + "\n## ══════════════ END OF CONNECTOR GUIDELINES ══════════════\n\n"
            + _base_test_gen
        )

    methods_list = "\n".join(f"- {m}" for m in methods)

    # When merging into existing tests, prepend the existing file content as context
    existing_tests_block = ""
    if existing_tests and not reset:
        existing_tests_block = (
            "## ══════════════════════════════════════════════════════════\n"
            "## EXISTING test_connector.py — PRESERVE ALL OTHER TEST CLASSES\n"
            "## ══════════════════════════════════════════════════════════\n"
            "CRITICAL: The file below already contains tests for OTHER methods.\n"
            "You MUST preserve every test class NOT listed in 'Test ONLY these methods' below.\n"
            "Only ADD or REPLACE test classes for the methods explicitly listed.\n"
            "Write the COMPLETE merged file (existing tests + new tests).\n\n"
            + existing_tests
            + "\n## ══════════════ END OF EXISTING TESTS ══════════════\n\n"
        )

    initial_message = (
        _agentic_ground_truth_block
        + existing_tests_block
        + f"Generate pytest unit tests for the connector in: {connector_dir.name}/\n\n"
        f"Test ONLY these methods:\n{methods_list}\n\n"
        f"Class name hint: {class_name} (verify by reading connector.py)\n\n"
        f"IMPORTANT — follow the CONNECTOR-SPECIFIC TEST GUIDELINES above EXACTLY:\n"
        f"  • Use the Fixture Blueprint from Section 5 — copy it precisely\n"
        f"  • Use the Mock Patterns from Section 6 — exact return value shapes\n"
        f"  • For each method in Section 9, implement ALL listed test scenarios\n"
        f"  • Copy the Required Assertions from Section 9 exactly\n\n"
        f"Steps:\n"
        f"0. Call check_imports() FIRST — if it reports errors, fix those files before doing anything else.\n"
        f"   Only proceed when check_imports() returns OK.\n"
        f"1. Read connector.py to verify exact signatures and logic\n"
        f"2. Call validate_connector_rules() — if violations exist, fix connector.py FIRST before writing tests\n"
        f"   ⚠ CONNECTOR FIX RULE: make MINIMAL targeted changes only — fix the specific violation, do NOT rewrite the entire connector.py\n"
        f"3. Read client/*.py to confirm which methods are async def\n"
        f"4. Write tests/test_connector.py — {'include ALL existing test classes unchanged + add new classes for listed methods' if existing_tests and not reset else 'use Section 5 fixture, Section 9 specs per method'}\n"
        f"5. validate_python → fix any syntax errors\n"
        f"6. run_tests → analyse failures carefully:\n"
        f"   • If failures come from connector.py (e.g. install() making network calls, wrong logger, wrong field names) → fix ONLY the failing lines in connector.py, do NOT rewrite the file\n"
        f"   • If failures are test setup issues (wrong mock, missing patch, wrong assertion) → fix the test\n"
        f"   • NEVER try to patch tests to hide a connector bug — fix the root cause\n"
        f"7. done(summary) when all tests pass\n\n"
        f"IMPORTANT: mock set_token, get_token, ingest_batch in every test that calls "
        f"install/authorize/sync — they connect to a real DB and will hang without mocks.\n\n"
        f"## Connector bug patterns to recognise and fix in connector.py (MINIMAL fix only):\n"
        f"- install() calling get_transaction_status/health_check/any API → remove the network call, just validate config keys\n"
        f"- `import logging; logger = logging.getLogger(...)` → replace with `import structlog; logger = structlog.get_logger(__name__)`\n"
        f"- ConnectorStatus() missing connector_id= → add connector_id=self.connector_id\n"
        f"- result.status used as enum (ConnectorStatus.SUCCESS) → use result.health and result.auth_status\n"
        f"- os.getenv() for credentials → use self.config.get(key)\n"
        f"- ⛔ AuthStatus.AUTHORIZED / UNAUTHORIZED / UNKNOWN / OK / ACTIVE do NOT exist — use AUTHENTICATED or CONNECTED instead\n"
        f"  Valid AuthStatus values: PENDING, CONNECTED, EXPIRED, FAILED, MISSING_CREDENTIALS, TOKEN_EXPIRED, AUTHENTICATED, INVALID_CREDENTIALS\n"
        f"- ⛔ exceptions.py using Optional without `from typing import Optional` → add the import at the top"
    )

    result = await _gemini_agentic_loop(
        connector_dir,
        system_prompt=system_prompt,
        initial_message=initial_message,
        tools=_FIX_TOOLS,        # includes run_tests + read/write/validate/list/done + search_knowledge
        log_cb=log_cb,
        max_iterations=20,       # extra headroom: may need to fix connector.py AND tests
        stop_on_done=True,
        stop_on_tests_pass=True,
        protected_files={"connector.py"},  # never overwrite the connector source during test-gen
    )

    # Final structured pytest run for pass/fail counts (cancellable)
    # Trust the loop's success=True — only check final run if loop didn't confirm success.
    # Re-running can produce false-negatives (e.g. "error" substring in DeprecationWarning paths).
    if not result.get("success"):
        final_output = await _run_tests_async(connector_dir)
        result["pytest_output"] = final_output
        result["success"] = _tests_passed(final_output)
    return result


async def gemini_agentic_fix(
    connector_dir: Path,
    *,
    initial_pytest_output: str,
    knowledge_fn: KnowledgeQueryFn = None,
    tenant_id: str = "",
    provider: str = "",
    service: str = "",
    test_guidelines: str = "",
    log_cb: LogCallback = None,
    max_iterations: int = 40,
    target_methods: list[str] | None = None,
    compile_errors: str = "",
) -> Dict[str, Any]:
    """Fix loop: Gemini reads files, writes fixes, runs pytest, iterates until tests pass.

    `test_guidelines` — the connector-specific test_guidelines.md content (from local disk
    or R2).  When provided it is prepended to the system prompt so Gemini honours the
    exact fixture blueprint, mock patterns, and per-method assertions while fixing.
    """
    global _active_knowledge_fn
    _active_knowledge_fn = knowledge_fn

    # Ingest all connector files into KB so search_knowledge returns real method signatures
    if knowledge_fn and tenant_id and provider and service:
        await _ingest_connector_files(connector_dir, tenant_id, provider, service, log_cb)

    # Build system prompt: inject connector-specific test guidelines at the top so Gemini
    # cannot ignore them.  The guidelines describe exact fixture blueprints, mock wiring
    # expectations, and per-method scenarios — critical context when fixing broken tests.
    _base_system = await _r2_service.get_step_prompt("FIX_SYSTEM", _FIX_SYSTEM)
    if test_guidelines and len(test_guidelines.strip()) > 300:
        _system_prompt = (
            "## CONNECTOR-SPECIFIC TEST GUIDELINES — READ BEFORE FIXING\n"
            "These guidelines define the REQUIRED fixture blueprint, mock setup patterns,\n"
            "and per-method test scenarios for this connector. When fixing test failures,\n"
            "all fixes MUST comply with these guidelines — do not invent different patterns.\n\n"
            + test_guidelines.strip()
            + "\n\n## END OF CONNECTOR TEST GUIDELINES\n\n"
            + _base_system
        )
    else:
        _system_prompt = _base_system

    # ── Write compile_errors.md so Gemini can read it as a file ────────────
    _compile_error_block = ""
    if compile_errors.strip():
        _compile_errors_file = connector_dir / "compile_errors.md"
        _compile_errors_file.write_text(
            f"# Compile Errors — fix these FIRST\n\n{compile_errors.strip()}\n",
            encoding="utf-8",
        )
        _compile_error_block = (
            "## 🚨 CRITICAL — COMPILE ERRORS DETECTED (fix these BEFORE anything else)\n"
            f"{compile_errors.strip()}\n\n"
            "These are compile-time errors in the connector source files. They will cause ALL tests\n"
            "to fail with collection errors. You MUST fix them first:\n"
            "- Read the file mentioned in the error.\n"
            "- Apply ONLY the minimal fix (e.g. add a missing import line).\n"
            "- Do NOT rewrite the file — make a surgical, targeted change.\n"
            "- Call check_imports() to confirm the fix, then proceed to test failures.\n\n"
        )

    initial_message = (
        _compile_error_block
        + f"Tests are failing in: {connector_dir.name}/\n\n"
        "## MANDATORY FIRST STEPS — FOLLOW IN ORDER, DO NOT SKIP\n"
        "1. Call `check_imports()` FIRST — verifies the full import chain works (exceptions → connector).\n"
        "   If it reports ANY error (NameError, ImportError, missing typing import, etc.) fix that file first.\n"
        "   Call `check_imports()` again after fixing — only proceed to step 2 when it returns OK.\n"
        "2. Read `test_failures.md` — full pytest output with line numbers.\n"
        "3. Check for PRIORITY 0 issues first: bug-documenting assertions, AsyncMock in side_effect, "
        "   import path mismatches between connector.py and tests/test_connector.py.\n"
        "4. Read `connector.py` — class name, method signatures, return types, exceptions, attributes.\n"
        "5. Read `tests/test_connector.py` — current test structure.\n"
        "6. Read `exceptions.py` — check custom exception classes have `self.message = message` AND all imports present.\n"
        "7. Read any helper files (helpers/, client/, etc.) referenced by connector.py.\n"
        "Only after steps 1-7 should you write any fixes.\n\n"
        f"Current pytest output (also in test_failures.md):\n```\n{initial_pytest_output}\n```\n\n"
        "## FIX RULES\n"
        "- ⛔ NEVER use write_file for connector.py, exceptions.py, or client/ files — use patch_file ONLY.\n"
        "  patch_file takes `old_code` (exact lines to replace) and `new_code` (replacement). "
        "  Only those lines change — everything else stays identical.\n"
        "- write_file is ONLY for tests/test_connector.py when substantial test restructuring is needed.\n"
        "- Fix tests when they have bug-documenting assertions or wrong mock setup (see PRIORITY 0).\n"
        "- Fix connector.py when tests assert correct behavior that the connector doesn't implement.\n"
        "- Use ONLY classes, methods, and attributes that actually exist in connector.py.\n"
        "- ConnectorStatus fields: `health`, `auth_status`, `connector_id`, `message` — no `.status`.\n"
        "- ⛔ AuthStatus VALID values ONLY: PENDING, CONNECTED, EXPIRED, FAILED, MISSING_CREDENTIALS, TOKEN_EXPIRED, AUTHENTICATED, INVALID_CREDENTIALS.\n"
        "  AuthStatus.AUTHORIZED / UNAUTHORIZED / UNKNOWN / OK / ACTIVE do NOT exist — they raise AttributeError at runtime.\n"
        "- ⛔ exceptions.py: MUST have `from typing import Optional` if Optional is used in any type hint.\n"
        "- ⛔ Mock patch paths: ALWAYS patch where the name is USED, not where it is defined.\n"
        "  e.g. connector.py does `from client.gmail_client import GmailClient` → patch as `connector.GmailClient`.\n"
        "  NEVER patch `client.gmail_client.GmailClient` — that patches the source, connector.py ignores it.\n"
        + (
            "- The system prompt contains connector-specific test guidelines. "
            "Follow the fixture blueprint and mock patterns exactly as described there.\n"
            if test_guidelines and len(test_guidelines.strip()) > 300
            else ""
        )
        + (
            f"\n\n⛔ SCOPE: You are ONLY fixing methods: {', '.join(target_methods)}. "
            "run_tests() is filtered to these methods only — other methods are not your concern. "
            "Do NOT rewrite or delete test functions for other methods. Surgical fixes only."
            if target_methods else ""
        )
        + "\nRun tests after each fix, iterate until ALL targeted tests pass, then call done(summary)."
    )

    result = await _gemini_agentic_loop(
        connector_dir,
        system_prompt=_system_prompt,
        initial_message=initial_message,
        tools=_FIX_TOOLS,
        log_cb=log_cb,
        max_iterations=max_iterations,
        stop_on_done=True,
        stop_on_tests_pass=True,
        protected_files=None,
        target_methods=target_methods,
    )

    # Final structured pytest run for pass/fail counts (cancellable)
    # Trust the loop's success=True — only check final run if loop didn't confirm success.
    if not result.get("success"):
        final_output = await _run_tests_async(connector_dir, methods=target_methods)
        result["pytest_output"] = final_output
        result["success"] = _tests_passed(final_output)
    return result


async def gemini_agentic_fix_connector(
    connector_dir: Path,
    *,
    initial_pytest_output: str,
    knowledge_fn: KnowledgeQueryFn = None,
    tenant_id: str = "",
    provider: str = "",
    service: str = "",
    test_guidelines: str = "",
    log_cb: LogCallback = None,
    max_iterations: int = 40,
    target_methods: list[str] | None = None,
) -> Dict[str, Any]:
    """Fix loop: Gemini fixes connector.py (NOT tests) to make the existing tests pass.

    `tests/test_connector.py` is treated as the frozen source of truth.
    Gemini reads the failing tests, understands what they expect, and fixes
    connector.py / client/ files to satisfy those expectations.

    `test_guidelines` — connector-specific guidelines (from test_guidelines.md).
    Injected into the system prompt so Gemini knows the exact client attribute names,
    which methods are async, expected return shapes, and mock targets — critical context
    when fixing the connector implementation to satisfy test expectations.
    """
    global _active_knowledge_fn
    _active_knowledge_fn = knowledge_fn

    if knowledge_fn and tenant_id and provider and service:
        await _ingest_connector_files(connector_dir, tenant_id, provider, service, log_cb)

    _base_system = await _r2_service.get_step_prompt("CONNECTOR_FIX_SYSTEM", _CONNECTOR_FIX_SYSTEM)
    if test_guidelines and len(test_guidelines.strip()) > 300:
        _system_prompt = (
            "## CONNECTOR-SPECIFIC TEST GUIDELINES — READ BEFORE FIXING\n"
            "These guidelines describe the exact client attribute names, which methods are async,\n"
            "expected return shapes, fixture blueprints, and per-method test scenarios.\n"
            "Use them to understand WHAT the tests expect so you can fix connector.py accordingly.\n\n"
            + test_guidelines.strip()
            + "\n\n## END OF CONNECTOR TEST GUIDELINES\n\n"
            + _base_system
        )
    else:
        _system_prompt = _base_system

    initial_message = (
        f"Tests are failing in: {connector_dir.name}/\n\n"
        "## MANDATORY FIRST STEPS — DO THESE BEFORE WRITING ANY FIX\n"
        "1. Read `tests/test_connector.py` — check for PRIORITY 0 issues first: bug-documenting "
        "assertions, wrong mock setups (AsyncMock in side_effect), import path mismatches.\n"
        "2. Read `connector.py` — compare the current implementation against test expectations.\n"
        "3. Read every file in `client/` — understand the real API client signatures.\n"
        "4. Read `exceptions.py` — check custom exception classes have `self.message = message`.\n"
        "5. Read `helpers/` files if connector.py imports from them.\n"
        "Only after reading these files should you write any fixes.\n\n"
        f"Current pytest output:\n```\n{initial_pytest_output}\n```\n\n"
        "## RULES\n"
        "- You MAY edit ANY file: `connector.py`, `client/` files, `exceptions.py`, "
        "AND `tests/test_connector.py` if the tests themselves are wrong (bug-documenting assertions, "
        "wrong mock setup, import path mismatch — see PRIORITY 0 in system prompt).\n"
        "- Do not delete connector methods; fix their implementations.\n"
        "- Match return types, attribute names, and exception types exactly as correct behavior requires.\n"
        + (
            "- The system prompt contains connector-specific test guidelines. "
            "Use the client layer description, mock targets, and per-method specs to understand "
            "exactly what the tests expect from the connector implementation.\n"
            if test_guidelines and len(test_guidelines.strip()) > 300
            else ""
        )
        + (
            f"\n\n⛔ SCOPE: Fixing methods: {', '.join(target_methods)}. "
            "run_tests() is filtered to these methods. Do not touch connector methods outside this scope."
            if target_methods else ""
        )
        + "\nRun tests after each fix, iterate until ALL targeted tests pass, then call done(summary)."
    )

    result = await _gemini_agentic_loop(
        connector_dir,
        system_prompt=_system_prompt,
        initial_message=initial_message,
        tools=_FIX_TOOLS,
        log_cb=log_cb,
        max_iterations=max_iterations,
        stop_on_done=True,
        stop_on_tests_pass=True,
        protected_files=None,
        target_methods=target_methods,
    )

    if not result.get("success"):
        final_output = await _run_tests_async(connector_dir, methods=target_methods)
        result["pytest_output"] = final_output
        result["success"] = _tests_passed(final_output)
    return result
